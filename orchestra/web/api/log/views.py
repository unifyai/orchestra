"""

Includes endpoints related to entries.
"""

import json
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from json import JSONDecodeError
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from fastapi import (
    APIRouter,
    Body,
    Depends,
    HTTPException,
    Path,
    Query,
    Request,
    status,
)
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import (
    INTEGER,
    TIMESTAMP,
    Date,
    DateTime,
    Float,
    Integer,
    Interval,
    String,
    Time,
    and_,
    asc,
    case,
    cast,
    desc,
    exists,
    func,
    literal,
    select,
    tuple_,
)
from sqlalchemy.dialects.postgresql import BOOLEAN, JSONB
from sqlalchemy.orm import aliased
from sqlalchemy.sql import text
from sqlalchemy.sql.selectable import Subquery

from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dao.derived_log_dao import DerivedLogDAO
from orchestra.db.dao.field_type_dao import FieldTypeDAO
from orchestra.db.dao.log_dao import ImmutableFieldError, LogDAO, OverwriteError
from orchestra.db.dao.log_event_dao import LogEventDAO
from orchestra.db.dao.project_dao import ProjectDAO
from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import (
    ActiveDerivedLog,
    Context,
    DerivedLog,
    Log,
    LogEvent,
    LogEventContext,
    LogHistory,
)
from orchestra.web.api.dependencies import auth_admin_key
from orchestra.web.api.log.schema import (
    CreateDerivedEntriesConfig,
    CreateLogConfig,
    DeleteLogEntryRequest,
    GetLogsMetricRequest,
    RenameFieldRequest,
    UpdateDerivedEntriesConfig,
    UpdateLogRequest,
)
from orchestra.web.api.utils.helpers import CustomEncoder
from orchestra.web.api.utils.http_responses import not_found

from .helpers import (
    STR_TO_SQL_TYPES,
    _compute_expression,
    _extract_placeholders,
    _flatten_fields,
    _format_flat_logs,
    _get_final_logs,
    _substitute_placeholders,
    build_sql_query,
    is_image_field,
    str_filter_exp_to_dict,
)

router = APIRouter()

# Admin router for protected endpoints
admin_router = APIRouter()


# Sorting configuration modes
class SortType(str, Enum):
    WITHIN_GROUPS = "within_groups"
    SORT_GROUPS = "sort_groups"


class SortDirection(str, Enum):
    ASCENDING = "ascending"
    DESCENDING = "descending"


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


###########################
# endpoints
###########################
def create_logs_internal(
    request: CreateLogConfig,
    project_id: int,
    context_id: int,
    project_dao: ProjectDAO,
    field_type_dao: FieldTypeDAO,
    log_event_dao: LogEventDAO,
    log_dao: LogDAO,
    context_dao: ContextDAO,
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
            # Extract mutable flag from explicit_types if present
            mutable = (
                explicit_types.get(field_name, {}).get("mutable", False)
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
                field_category="param" if is_param else "entry",
                context_id=context_id,
            )

    # Bulk create all log events at once
    entries_len = len(entries_list)
    params_len = len(params_list)
    total_logs = max(entries_len, params_len)

    # Bulk create all log events in one operation
    log_event_ids = log_event_dao.bulk_create(
        project_id=project_id,
        context_id=context_id,
        count=total_logs,
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

        # Extract explicit types
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
                # If in a versioned context, force mutable=True
                if context_id and context_dao.is_versioned(context_id):
                    mutable = True
                new_field_types.append(
                    {
                        "project_id": project_id,
                        "field_name": k,
                        "value": v,
                        "mutable": mutable,
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
                version = existing_param[0][0].version
            else:
                version = log_dao.get_next_param_version(project_id, context_id, k)

            # Add to records for bulk creation
            log_records_to_create.append(
                {
                    "project_id": project_id,
                    "log_event_id": log_event_id,
                    "key": k,
                    "value": v,
                    "version": version,
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
                # If in a versioned context, force mutable=True
                if context_id and context_dao.is_versioned(context_id):
                    mutable = True
                new_field_types.append(
                    {
                        "project_id": project_id,
                        "field_name": k,
                        "value": v,
                        "mutable": mutable,
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
    if new_field_types:
        field_type_dao.bulk_create_field_types(new_field_types)

    # Bulk create all log records
    log_dao.bulk_create(log_records_to_create)

    # Check for duplicates if context doesn't allow duplicates
    context_obj = None
    if context_id:
        context_obj = (
            context_dao.session.query(Context).filter_by(id=context_id).first()
        )
        # Check if context doesn't allow duplicates
        if context_obj and not context_obj.allow_duplicates:
            for log_event_id in log_event_ids:
                # Check for duplicates
                duplicate = context_dao.check_for_duplicates(context_id, log_event_id)
                if duplicate:
                    log_event_dao.delete(log_event_id)
                    raise HTTPException(
                        status_code=400,
                        detail=f"Duplicate log detected in context '{context_obj.name}' which doesn't allow duplicates. Log event ID: {log_event_id}",
                    )
    if context_obj and context_obj.is_versioned:
        # archive the new state
        context_dao.archive_context_state(
            context_obj,
            name="create",
            description=f"Created {total_logs} new LogEvents",
        )
        context_obj.version += 1
        context_obj.updated_at = datetime.now(timezone.utc)
        context_dao.session.commit()

    return log_event_ids


@router.post(
    "/logs",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {
                        "info": "Logs created successfully!",
                        "log_event_ids": [1, 2, 3],
                    },
                },
            },
        },
        404: {
            "description": "Project Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Project not found.",
                    },
                },
            },
        },
    },
)
def create_logs(
    request_fastapi: Request,
    request: CreateLogConfig,
    project_dao: ProjectDAO = Depends(),
    field_type_dao: FieldTypeDAO = Depends(),
    log_event_dao: LogEventDAO = Depends(),
    log_dao: LogDAO = Depends(),
    context_dao: ContextDAO = Depends(),
):
    """
    Creates one or more logs associated to a project. Logs are
    LLM-call-level data that might depend on other variables.

    If a context is specified and it is versioned, all logs will be versioned
    and mutable. The context version will be incremented automatically when
    logs are added, updated, or removed.

    The context parameter can be:
    - A string: Uses the string as the context name with default values (description=None, is_versioned=False)
    - An object: Uses the object's name, description, and is_versioned properties

    An "explicit_types" dictionary can be passed as part of the `entries`.
    If present, any matching key inside this dictionary will override the
    inferred type of that particular entry. The explicit_types dictionary
    can also specify if a field is mutable via a 'mutable' boolean flag:

    {
        "field_name": {
            "type": "str",
            "mutable": false  # Makes the field immutable
        }
    }

    By default, all fields are immmutable unless specified otherwise.
    Once a field is marked as mutable, only then can it be modified through
    the update endpoint.

    This method returns the ids of the new stored logs.
    """
    # check if the project exists
    try:
        user_id = request_fastapi.state.user_id
        project = project_dao.get_by_user_and_name(
            user_id=user_id,
            name=request.project,
        )
        project_id = project.id
    except (IndexError, AttributeError):
        raise not_found("Project")

    # Get or create context_id
    if request.context:
        # Check if context is a string
        if isinstance(request.context, str):
            context_id = context_dao.get_or_create(
                project_id,
                name=request.context,
                description=None,
                is_versioned=False,
            )
        else:
            context_id = context_dao.get_or_create(
                project_id,
                name=request.context.name,
                description=request.context.description,
                is_versioned=request.context.is_versioned,
            )
    else:
        # get the default context
        context_id = context_dao.get_or_create(project_id, name="")

    # Call the internal implementation with validated project and context
    return create_logs_internal(
        request=request,
        project_id=project_id,
        context_id=context_id,
        project_dao=project_dao,
        field_type_dao=field_type_dao,
        log_event_dao=log_event_dao,
        log_dao=log_dao,
        context_dao=context_dao,
    )


def unify_id_sets_by_subset(alias_id_sets: Dict[str, Set[int]]) -> Dict[str, Set[int]]:
    """
    Applies a 3-step logic:
      1) If all sets are the same size, do nothing.
      2) Else, pick the smallest set S_min. If S_min is a subset of every other set,
         then reduce every alias to S_min. Otherwise, raise HTTP 400 error.
    """
    if not alias_id_sets:
        return alias_id_sets

    all_sets = list(alias_id_sets.values())
    lengths = [len(s) for s in all_sets]

    # 1) If all sets have the same length, do nothing.
    if len(set(lengths)) == 1:
        # They are already consistent, so no changes needed.
        return alias_id_sets

    # 2) Identify the smallest set
    smallest = min(all_sets, key=len)

    # Check if smallest is a subset of each other set
    for s in all_sets:
        if not smallest.issubset(s):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "Mismatch in referenced log IDs: no single subset can unify them.\n"
                    f"Smallest set={smallest}, but found disjoint set={s}."
                ),
            )

    # If we get here, it's safe to unify everything to that smallest set
    for alias in alias_id_sets:
        alias_id_sets[alias] = smallest

    return alias_id_sets


def prepare_resolved_ids(
    equation: str,
    referenced_logs: Dict[str, Union[List[int], Dict[str, Any]]],
    request_fastapi: Request,
    project_name: str,
    project_dao,
    field_type_dao,
    context_dao,
    session,
) -> Dict[str, List[int]]:
    """
    1) Parses `equation` to find placeholders.
    2) Groups them by 'alias' (the part before the colon).
    3) If user gave a direct list for that alias, just use it.
       Otherwise, if user gave a dict, run one or more `_get_logs_query` calls:
         - For each subfield in the placeholders for that alias,
           do a query with from_fields=<subfield>, then intersect the results.
         - If the placeholder is just {alias} (no subfield),
           do a single query without forcing from_fields.
    4) Finally, do a global intersection across all aliases
       so that each alias has exactly the same set of IDs.
       (This ensures no mismatch lengths.)
    5) Return a dict of shape: { alias -> sorted_list_of_ids }
    """
    placeholders = _extract_placeholders(equation)
    # Step 1: Group placeholders by alias
    # e.g. "Table:gender" => alias="Table", subfield="gender"
    #      "Table:nationality" => alias="Table", subfield="nationality"
    alias_to_subfields = defaultdict(set)

    for ph in placeholders:
        if ":" in ph:
            alias, subfield = ph.split(":", 1)
            alias_to_subfields[alias].add(subfield)
        else:
            # e.g. placeholder is just {Table}, no subfield
            alias_to_subfields[ph]

    alias_id_sets: Dict[str, Set[int]] = {}

    # Step 2: For each alias, figure out the set of IDs
    for alias, subfields in alias_to_subfields.items():
        user_val = referenced_logs.get(alias)

        # A) If user gave a direct list: just convert to set
        if isinstance(user_val, list):
            alias_id_sets[alias] = set(user_val)
            continue

        # B) Otherwise, user_val might be a dict or None
        base_dict = user_val if isinstance(user_val, dict) else {}

        # If there are NO subfields => placeholder was {alias}, no : part
        if not subfields:
            # We do exactly one query using base_dict
            raw_rows, _, _count = _get_logs_query(
                request_fastapi=request_fastapi,
                project=project_name,
                column_context=base_dict.get("column_context"),
                context=base_dict.get("context"),
                filter_expr=base_dict.get("filter_expr"),
                sorting=base_dict.get("sorting"),
                from_ids=base_dict.get("from_ids"),
                exclude_ids=base_dict.get("exclude_ids"),
                from_fields=base_dict.get("from_fields"),
                exclude_fields=base_dict.get("exclude_fields"),
                limit=base_dict.get("limit"),
                offset=base_dict.get("offset", 0),
                project_dao=project_dao,
                field_type_dao=field_type_dao,
                context_dao=context_dao,
                session=session,
            )
            le_ids = {r[7] for r in raw_rows}  # r[7] is log_event_id
            alias_id_sets[alias] = le_ids
        else:
            # We have one or more subfields: {alias:subfield}
            # For each subfield, do a query with from_fields=<subfield> (plus user filters), then intersect.
            combined_ids = None
            for sf in subfields:
                # Make a shallow copy so we don't overwrite the original
                query_dict = dict(base_dict)

                # If user didn't explicitly set from_fields, set it to subfield
                if "from_fields" not in query_dict:
                    query_dict["from_fields"] = sf
                else:
                    # If they already have from_fields, you might decide either:
                    # - Overwrite it with sf,
                    # - OR combine with an "&" if you want logs that have both fields.
                    #
                    # Here we simply overwrite if we want logs only for that subfield.
                    # If you prefer to combine them, do something like:
                    # query_dict["from_fields"] += f"&{sf}"
                    query_dict["from_fields"] = sf

                raw_rows, _, _count = _get_logs_query(
                    request_fastapi=request_fastapi,
                    project=project_name,
                    column_context=query_dict.get("column_context"),
                    context=query_dict.get("context"),
                    filter_expr=query_dict.get("filter_expr"),
                    sorting=query_dict.get("sort"),
                    from_ids=query_dict.get("from_ids"),
                    exclude_ids=query_dict.get("exclude_ids"),
                    from_fields=query_dict.get("from_fields"),
                    exclude_fields=query_dict.get("exclude_fields"),
                    limit=query_dict.get("limit"),
                    offset=query_dict.get("offset", 0),
                    project_dao=project_dao,
                    field_type_dao=field_type_dao,
                    context_dao=context_dao,
                    session=session,
                )
                le_ids = {r[7] for r in raw_rows}

                if combined_ids is None:
                    combined_ids = le_ids
                else:
                    combined_ids = combined_ids.intersection(le_ids)

            alias_id_sets[alias] = combined_ids if combined_ids else set()

    # Step 3: Fix mismatch lengths by a global intersection:
    # If we have multiple aliases, each has its own set. Intersect them so they match.
    alias_id_sets = unify_id_sets_by_subset(alias_id_sets)
    # That function either raises 400 or updates
    # the sets so they are all the same size or they remain as is if they're already equal length.

    # Convert to sorted lists
    resolved_ids: Dict[str, List[int]] = {
        alias: sorted(id_set) for alias, id_set in alias_id_sets.items()
    }
    return resolved_ids


@router.post(
    "/logs/derived",
    responses={
        200: {
            "description": "Derived log entries created successfully.",
            "content": {
                "application/json": {
                    "example": {
                        "info": "Created 3 derived logs with key='example_key'.",
                        "derived_log_ids": [101, 102, 103],
                    },
                },
            },
        },
        404: {
            "description": "Project Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Project 'example_project' not found.",
                    },
                },
            },
        },
        400: {
            "description": "Bad Request",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "All referenced log lists must have the same length. Found lengths: [2, 3].",
                    },
                },
            },
        },
    },
)
def create_derived_entry(
    request_fastapi: Request,
    body: CreateDerivedEntriesConfig,
    project_dao: ProjectDAO = Depends(),
    field_type_dao: FieldTypeDAO = Depends(),
    context_dao: ContextDAO = Depends(),
    derived_log_dao: DerivedLogDAO = Depends(),
    session=Depends(get_db_session),
):
    """
    Creates one or more derived-log entries based on `body.equation` and `body.referenced_logs`.
    Eagerly computes each derived value and stores it in DerivedLog.value.

    The context parameter can be:
    - A string: Uses the string as the context name with default values (description=None, is_versioned=False)
    - An object: Uses the object's name, description, and is_versioned properties
    """
    user_id = request_fastapi.state.user_id

    # 1) Validate the project
    try:
        project_obj = project_dao.get_by_user_and_name(
            name=body.project,
            user_id=user_id,
        )
        project_id = project_obj.id
    except (IndexError, AttributeError):
        raise HTTPException(
            status_code=404,
            detail=f"Project '{body.project}' not found.",
        )
    # Get or create context_id
    if body.context:
        # Check if context is a string
        if isinstance(body.context, str):
            context_id = context_dao.get_or_create(
                project_obj.id,
                name=body.context,
                description=None,
                is_versioned=False,
            )
        else:
            context_id = context_dao.get_or_create(
                project_obj.id,
                name=body.context.name,
                description=body.context.description,
                is_versioned=body.context.is_versioned,
            )
    else:
        # get the default context
        context_id = context_dao.get_or_create(project_obj.id, name="")

    # Check if this is a filter-based derived log
    is_filter_based = False
    filter_expression = None
    if isinstance(body.referenced_logs, dict):
        # Check if any value in referenced_logs is a dict with filter_expr
        for key, value in body.referenced_logs.items():
            if isinstance(value, dict) and "filter_expr" in value:
                is_filter_based = True
                filter_expression = body.referenced_logs
                break

    resolved_ids = prepare_resolved_ids(
        equation=body.equation,
        referenced_logs=body.referenced_logs,
        request_fastapi=request_fastapi,
        project_name=body.project,
        project_dao=project_dao,
        field_type_dao=field_type_dao,
        context_dao=context_dao,
        session=session,
    )

    # If none found, short‐circuit
    if not any(len(v) for v in resolved_ids.values()):
        return {"info": "No references found. Nothing to create."}
    created_derived_ids = []
    try:

        # 5) Build a filter_dict that references those base logs. Then compute
        filter_expr, alias_to_key_map = _substitute_placeholders(
            body.equation,
            resolved_ids,
        )
        field_types = field_type_dao.get_field_types(
            project_obj.id,
            context_id=context_id,
        )
        filter_dict = str_filter_exp_to_dict(
            filter_expr,
            field_names=list(field_types.keys()),
        )
        resolved_ids_dict = {}
        for key, ids in resolved_ids.items():
            resolved_ids_dict.setdefault(alias_to_key_map[key], []).extend(ids)
        # get the filtered log events
        log_event_ids_subq = (
            session.query(LogEvent.id)
            .filter(project_obj.id == LogEvent.project_id)
            .subquery(name="log_event_ids_subq")
        )
        computed_values = _compute_expression(
            filter_dict,
            LogEvent,
            session,
            log_event_ids=log_event_ids_subq,
        )

        # Create a new derived log entry for each computed value
        new_derived_logs = []
        placeholders = _extract_placeholders(body.equation)
        referenced_logs = {
            ph.split(":")[1]: v
            for ph in placeholders
            for k, v in body.referenced_logs.items()
            if k in ph
        }
        # Iterate over the computed values and resolved IDs
        for i, (_, value) in enumerate(computed_values):
            # Get all log IDs involved in this specific computation
            involved_log_ids = list(set(ids[i] for ids in resolved_ids.values()))

            # Create a derived entry for each log ID involved in this computation
            for log_event_id in involved_log_ids:
                val = json.loads(json.dumps(value, cls=CustomEncoder))
                inferred_type = LogDAO.infer_type("", val)
                new_derived_logs.append(
                    DerivedLog(
                        log_event_id=log_event_id,
                        key=body.key,
                        equation=body.equation,
                        referenced_logs=referenced_logs,
                        value=val,
                        inferred_type=inferred_type,
                        created_at=datetime.now(timezone.utc),
                        updated_at=datetime.now(timezone.utc),
                    ),
                )

        # Bulk insert all new derived logs in one go
        session.bulk_save_objects(new_derived_logs)

        # If this is a filter-based derived log, create an ActiveDerivedLog
        if is_filter_based:
            # Check if a template already exists for this project and key
            existing_template = (
                session.query(ActiveDerivedLog)
                .filter(
                    ActiveDerivedLog.project_id == project_obj.id,
                    ActiveDerivedLog.key == body.key,
                )
                .first()
            )

            if not existing_template:
                # Create a new template
                template = ActiveDerivedLog(
                    project_id=project_obj.id,
                    context_id=context_id,
                    key=body.key,
                    equation=body.equation,
                    referenced_logs=referenced_logs,
                    filter_expression=filter_expression,
                    inferred_type=inferred_type,
                    is_active=True,
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                )
                session.add(template)
            else:
                # Update existing template
                existing_template.equation = body.equation
                existing_template.referenced_logs = referenced_logs
                existing_template.filter_expression = filter_expression
                existing_template.inferred_type = inferred_type
                existing_template.is_active = True
                existing_template.updated_at = datetime.now(timezone.utc)

        session.commit()

        # Create or update field type record for derived entry
        field_type_dao.create_field_type_if_absent(
            project_id=project_obj.id,
            field_name=body.key,
            value=val,
            field_category="derived_entry",
            context_id=context_id,
        )

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create derived logs with key='{body.key}'. Error: {e}",
        )
    created_derived_ids = [log.id for log in new_derived_logs]
    return {
        "info": f"Created {len(created_derived_ids)} derived logs with key='{body.key}'.",
        "derived_log_ids": created_derived_ids,
    }


@router.put(
    "/logs/derived",
    responses={
        200: {
            "description": "Derived log updated successfully",
            "content": {
                "application/json": {
                    "example": {
                        "info": "Derived logs updated successfully!",
                    },
                },
            },
        },
        404: {
            "description": "Log Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "One or more logs with the specified IDs were not found.",
                    },
                },
            },
        },
        400: {
            "description": "Bad Request",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Invalid request format or data.",
                    },
                },
            },
        },
    },
)
def update_derived_log(
    request_fastapi: Request,
    body: UpdateDerivedEntriesConfig,
    derived_log_dao: DerivedLogDAO = Depends(),
    log_event_dao: LogEventDAO = Depends(),
    project_dao: ProjectDAO = Depends(),
    field_type_dao: FieldTypeDAO = Depends(),
    context_dao: ContextDAO = Depends(),
    session=Depends(get_db_session),
):
    """
    Updates multiple derived logs, identified either by a direct list of derived IDs or by
    get_logs–style filters. If 'referenced_logs' is provided, we delete all existing
    derived logs for each (log_event_id, key) group and re-insert new ones referencing
    the new base logs. Finally, we recompute them.

    The context parameter can be:
    - A string: Uses the string as the context name with default values (description=None, is_versioned=False)
    - An object: Uses the object's name, description, and is_versioned properties
    """
    user_id = request_fastapi.state.user_id

    # 1) Validate the project
    try:
        project_obj = project_dao.get_by_user_and_name(
            name=body.project,
            user_id=user_id,
        )
        project_id = project_obj.id
    except (IndexError, AttributeError):
        raise HTTPException(
            status_code=404,
            detail=f"Project '{body.project}' not found.",
        )
    # Get or create context_id
    if body.context:
        # Check if context is a string
        if isinstance(body.context, str):
            context_id = context_dao.get_or_create(
                project_obj.id,
                name=body.context,
                description=None,
                is_versioned=False,
            )
        else:
            context_id = context_dao.get_or_create(
                project_obj.id,
                name=body.context.name,
                description=body.context.description,
                is_versioned=body.context.is_versioned,
            )
    else:
        # get the default context
        context_id = context_dao.get_or_create(project_obj.id, name="")

    # 2) Resolve which DerivedLog IDs to update
    if isinstance(body.target_derived_logs, list):
        derived_log_ids = body.target_derived_logs
    else:
        # treat as get_logs argument, gather all derived log rows that match
        argdict = body.target_derived_logs
        raw_rows, _, _count = _get_logs_query(
            request_fastapi=request_fastapi,
            project=body.project,
            column_context=argdict.get("column_context"),
            context=argdict.get("context"),
            filter_expr=argdict.get("filter_expr"),
            sorting=argdict.get("sort"),
            from_ids=argdict.get("from_ids"),
            exclude_ids=argdict.get("exclude_ids"),
            from_fields=argdict.get("from_fields"),
            exclude_fields=argdict.get("exclude_fields"),
            limit=argdict.get("limit"),
            offset=argdict.get("offset", 0),
            project_dao=project_dao,
            field_type_dao=field_type_dao,
            context_dao=context_dao,
            session=session,
        )

        derived_event_ids = [
            row[7]
            for row in raw_rows
            if row[5] == "derived"  # row_source_type=="derived"
        ]
        if not derived_event_ids:
            return {"info": "No derived logs matched. Nothing to update."}

        derived_log_ids = [
            t[0]
            for t in session.query(DerivedLog.id)
            .filter(DerivedLog.log_event_id.in_(derived_event_ids))
            .all()
        ]

    if not derived_log_ids:
        return {"info": "No derived logs matched. Nothing to update."}

    # 3) Load the actual DerivedLog objects for these IDs
    matched_derived_logs = (
        session.query(DerivedLog).filter(DerivedLog.id.in_(derived_log_ids)).all()
    )

    if not matched_derived_logs:
        return {"info": "No derived logs matched. Nothing to update."}

    # 4) Verify user has permission
    valid_logs = []
    for dlog in matched_derived_logs:
        user_id_of_this_event = log_event_dao.get_user_id(id=dlog.log_event_id)
        if user_id_of_this_event != user_id:
            continue
        valid_logs.append(dlog)
    if not valid_logs:
        raise HTTPException(
            status_code=404,
            detail="No matching derived logs belong to your project or you lack permission.",
        )

    # 5) Group them by (log_event_id, old_key)
    group_map = defaultdict(list)
    for dlog in valid_logs:
        group_map[(dlog.log_event_id, dlog.key)].append(dlog)

    updated_equation = body.equation
    updated_key = body.key
    new_refs = body.referenced_logs  # can be None

    # If user *did not* pass new referenced_logs, do a simple "update in place"
    if not new_refs:
        # just update existing rows for new key/equation, then recompute
        for dlogs in group_map.values():
            for d in dlogs:
                try:
                    derived_log_dao.update(
                        id=d.id,
                        key=updated_key,
                        equation=updated_equation,
                    )
                except ValueError as ve:
                    raise HTTPException(status_code=400, detail=str(ve))
        # re-fetch them (some might have new key)
        updated_log_ids = [d.id for d in valid_logs]
        updated_objs = (
            session.query(DerivedLog).filter(DerivedLog.id.in_(updated_log_ids)).all()
        )
        # recompute
        derived_log_dao.recompute_derived_logs(
            logs_to_recompute=updated_objs,
            session=session,
            json_encoder=CustomEncoder,
        )
        return {"info": f"Updated {len(updated_objs)} derived logs successfully."}

    # 6) If new_refs *is* provided, do the "compute & insert" approach
    if new_refs:
        # Use updated_key/equation if provided; otherwise, take them from one of the matched logs.
        final_key = updated_key if updated_key else valid_logs[0].key
        final_equation = (
            updated_equation if updated_equation else valid_logs[0].equation
        )

        # Delete all derived logs that were matched by the update filter.
        valid_ids = [d.id for d in valid_logs]
        session.query(DerivedLog).filter(DerivedLog.id.in_(valid_ids)).delete(
            synchronize_session=False,
        )
        session.flush()  # flush the deletion so that new insertions do not conflict

        # Resolve the new referenced logs using prepare_resolved_ids
        resolved_ids = prepare_resolved_ids(
            equation=final_equation,
            referenced_logs=new_refs,
            request_fastapi=request_fastapi,
            project_name=body.project,
            project_dao=project_dao,
            field_type_dao=field_type_dao,
            context_dao=context_dao,
            session=session,
        )

        # If none found, short-circuit
        if not any(len(v) for v in resolved_ids.values()):
            return {"info": "No references found. Nothing to update."}

        # Get the common length of all resolved ID lists
        lengths = [len(lst) for lst in resolved_ids.values()]
        if lengths and len(set(lengths)) != 1:
            raise HTTPException(
                status_code=400,
                detail=f"All referenced log lists must have the same length. Found lengths: {lengths}",
            )

        # Compute derived values directly instead of creating empty logs and recomputing later
        # 1. Substitute placeholders to get filter expression and alias mapping
        filter_expr, alias_to_key_map = _substitute_placeholders(
            final_equation,
            resolved_ids,
        )

        # 2. Get field types for the project
        field_types = field_type_dao.get_field_types(
            project_id,
            context_id=context_id,
        )

        # 3. Build filter dictionary from the filter expression
        filter_dict = str_filter_exp_to_dict(
            filter_expr,
            field_names=list(field_types.keys()),
        )

        # 4. Create a dictionary mapping field keys to their resolved IDs
        resolved_ids_dict = {}
        for key, ids in resolved_ids.items():
            resolved_ids_dict.setdefault(alias_to_key_map[key], []).extend(ids)

        # 5. Get the filtered log events for this project
        log_event_ids_subq = (
            session.query(LogEvent.id)
            .filter(project_id == LogEvent.project_id)
            .subquery(name="log_event_ids_subq")
        )

        # 6. Compute the values directly using the filter dictionary
        computed_values = _compute_expression(
            filter_dict,
            LogEvent,
            session,
            log_event_ids=log_event_ids_subq,
        )

        # Create new derived logs with computed values
        new_derived_logs = []
        now = datetime.now(timezone.utc)
        placeholders = _extract_placeholders(body.equation)
        referenced_logs = {
            ph.split(":")[1]: v
            for ph in placeholders
            for k, v in body.referenced_logs.items()
            if k in ph
        }
        # Iterate over the computed values and resolved IDs
        for i, (_, value) in enumerate(computed_values):
            # Get all log IDs involved in this specific computation
            involved_log_ids = list(set(ids[i] for ids in resolved_ids.values()))

            # Create a derived entry for each log ID involved in this computation
            for log_event_id in involved_log_ids:
                # Convert value using CustomEncoder for proper JSON serialization
                val = json.loads(json.dumps(value, cls=CustomEncoder))
                inferred_type = LogDAO.infer_type("", val)

                new_derived_logs.append(
                    DerivedLog(
                        log_event_id=log_event_id,
                        key=final_key,
                        equation=final_equation,
                        referenced_logs=referenced_logs,
                        value=val,
                        inferred_type=inferred_type,
                        created_at=now,
                        updated_at=now,
                    ),
                )

        # Bulk insert all new derived logs in one go
        session.bulk_save_objects(new_derived_logs)
        session.commit()

        # Update the field type record for the derived entry
        field_type_dao.create_field_type_if_absent(
            project_id=project_id,
            context_id=context_id,
            field_name=final_key,
            mutable=True,
            value=val,  # Using the last computed value
            field_category="derived_entry",
        )

        return {
            "info": f"Updated references and replaced {len(valid_logs)} old derived logs with {len(new_derived_logs)} new ones.",
            "derived_log_ids": [obj.id for obj in new_derived_logs],
        }


@router.put(
    "/logs",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {"info": "Logs updated successfully!"},
                },
            },
        },
        404: {
            "description": "Log Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "One or more logs with the specified IDs were not found.",
                    },
                },
            },
        },
        400: {
            "description": "Bad Request",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Invalid request format or data.",
                    },
                },
            },
        },
    },
)
def update_logs(
    request_fastapi: Request,
    body: UpdateLogRequest,
    project_dao: ProjectDAO = Depends(),
    log_dao: LogDAO = Depends(),
    log_event_dao: LogEventDAO = Depends(),
    field_type_dao: FieldTypeDAO = Depends(),
    derived_log_dao: DerivedLogDAO = Depends(),
    context_dao: ContextDAO = Depends(),
    session=Depends(get_db_session),
):
    """
    Updates multiple logs with the provided entries. Each entry will be either added
    or overridden in the specified logs.

    A dictionary of "explicit_types" can be passed as part of the `entries`.
    If present, it will override the inferred type of any matching key in all logs.
    """
    # Validate all log IDs upfront
    not_found_logs = []
    log_id_to_project = {}  # Maps log_id -> project_id
    updated_ids = set()

    for log_id in body.ids:
        try:
            project_user_id, project_id = log_event_dao.get_user_and_project_id(
                id=log_id,
            )
            if (
                project_user_id != request_fastapi.state.user_id
            ):  # user is not the owner of the project
                # check if the user is a member of the organization this project belongs to
                project_obj = project_dao.filter_by_user_access(
                    user_id=request_fastapi.state.user_id,
                    id=project_id,
                )
                if not project_obj:
                    raise IndexError(
                        f"User {request_fastapi.state.user_id} does not have permission for log id {log_id}.",
                    )
            log_id_to_project[log_id] = project_id
        except IndexError:
            not_found_logs.append(log_id)
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"Unexpected error retrieving project info for log id {log_id}: {e}",
            )

    if not_found_logs:
        raise HTTPException(
            status_code=404,
            detail=(
                f"The following log ids were not found or permission was denied: {not_found_logs}. "
                "No updates were applied."
            ),
        )

    # Determine common context and fetch field types
    if len(set(log_id_to_project.values())) > 1:
        raise HTTPException(
            status_code=400,
            detail="All log IDs must belong to the same project for batch update.",
        )

    # Get the common project_id for all logs
    project_id = next(iter(log_id_to_project.values()))

    # Get or create context - support string, object, or list of strings
    if body.context:
        # Case 1: context is a list of strings
        if isinstance(body.context, list):
            # Create a list of context IDs
            ctx_ids = []
            for ctx in body.context:
                if isinstance(ctx, str):
                    # String context - get or create with default values
                    ctx_id = context_dao.get_or_create(
                        project_id,
                        name=ctx,
                        description=None,
                        is_versioned=False,
                    )
                    ctx_ids.append(ctx_id)
                else:
                    # Object context - use provided values
                    ctx_id = context_dao.get_or_create(
                        project_id,
                        name=ctx.name,
                        description=ctx.description,
                        is_versioned=ctx.is_versioned,
                    )
                    ctx_ids.append(ctx_id)

            # Use the first context for field types, but we'll update all contexts
            ctx_id = ctx_ids[0] if ctx_ids else None
        # Case 2: context is a string
        elif isinstance(body.context, str):
            ctx_id = context_dao.get_or_create(
                project_id,
                name=body.context,
                description=None,
                is_versioned=False,
            )
        # Case 3: context is an object (original behavior)
        else:
            ctx_id = context_dao.get_or_create(
                project_id,
                name=body.context.name,
                description=body.context.description,
                is_versioned=body.context.is_versioned,
            )
    else:
        # get the default context
        ctx_id = context_dao.get_or_create(project_id, name="")
        ctx_ids = [ctx_id] if ctx_id else []

    # Fetch field types once
    try:
        field_types = field_type_dao.get_field_types(
            project_id,
            return_mutable=True,
            context_id=ctx_id,
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to retrieve field types for project {project_id}: {e}",
        )

    # Prepare collections for bulk operations
    all_updates = []
    new_field_types = []
    updates_by_log_id = {}  # For context versioning

    # Process both params and entries
    for data_type in ("params", "entries"):
        data = getattr(body, data_type)

        for i, log_id in enumerate(body.ids):
            # Extract the data for this log. Support both dict and list formats.
            try:
                this_data = data if isinstance(data, dict) else data[i]
            except IndexError:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Mismatch between number of log ids ({len(body.ids)}) and length of "
                        f"{data_type} (got {len(data)}) at log id {log_id}."
                    ),
                )

            # Remove explicit types if provided, which override inferred types.
            explicit_types = this_data.pop("explicit_types", {})

            # Track this log for context versioning
            updates_by_log_id[log_id] = updates_by_log_id.get(log_id, 0) + 1

            # If only explicit_types are provided, update mutability.
            if not this_data:
                for k, v in explicit_types.items():
                    mutable_setting = v.get("mutable", False)
                    try:
                        field_type_dao.update_field_mutability(
                            project_id,
                            k,
                            mutable=mutable_setting,
                            context_id=ctx_id,
                        )
                    except Exception as e:
                        raise HTTPException(
                            status_code=400,
                            detail=f"Failed to update mutability for field '{k}' in log id {log_id}: {e}",
                        )

            # Process each field in the provided data.
            for k, v in this_data.items():
                if k in field_types:
                    expected_type = field_types[k]["field_type"]
                    original_type = LogDAO.infer_type(k, v)
                    if expected_type == "NoneType":
                        # undefined types are by-default mutable
                        try:
                            field_type_dao.upsert_field_type(
                                project_id,
                                k,
                                v,
                                mutable=True,
                                context_id=ctx_id,
                            )
                        except Exception as e:
                            raise HTTPException(
                                status_code=400,
                                detail=f"Error upserting field type for '{k}' in log id {log_id}: {e}",
                            )
                    elif original_type != expected_type and original_type != "NoneType":
                        raise HTTPException(
                            status_code=400,
                            detail=(
                                f"Type mismatch for field '{k}' in log id {log_id}: "
                                f"expected {expected_type}, got {original_type}"
                            ),
                        )
                else:
                    # For new fields, record the field along with its mutability setting.
                    mutable = (
                        explicit_types.get(k, {}).get("mutable", False)
                        if explicit_types
                        else False
                    )
                    category = "entry" if data_type == "entries" else "param"
                    new_field_types.append(
                        {
                            "project_id": project_id,
                            "field_name": k,
                            "value": v,
                            "mutable": mutable,
                            "field_category": category,
                            "context_id": ctx_id,
                        },
                    )

                # Compute the version based on whether we're handling params or entries.
                version = None
                if data_type == "params":
                    existing = log_dao.filter(
                        key=k,
                        value=json.dumps(v),
                        project_id=project_id,
                    )
                    if existing:
                        version = existing[0][0].version
                    else:
                        version = log_dao.get_next_param_version(project_id, ctx_id, k)

                # Add to the batch update list
                # If we have multiple contexts, create an update for each context
                if "ctx_ids" in locals() and ctx_ids:
                    for context_id in ctx_ids:
                        all_updates.append(
                            {
                                "log_event_id": log_id,
                                "key": k,
                                "value": v,
                                "version": version,
                                "explicit_types": explicit_types,
                                "field_types": field_types,
                                "context_id": context_id,
                                "overwrite": body.overwrite,
                            },
                        )
                else:
                    all_updates.append(
                        {
                            "log_event_id": log_id,
                            "key": k,
                            "value": v,
                            "version": version,
                            "explicit_types": explicit_types,
                            "field_types": field_types,
                            "context_id": ctx_id,
                            "overwrite": body.overwrite,
                        },
                    )
                updated_ids.add((k, log_id))

    # Bulk create any new field types
    if new_field_types:
        field_type_dao.bulk_create_field_types(new_field_types)

    # Bulk update all logs
    if all_updates:
        try:
            log_dao.bulk_update(all_updates, field_types=field_types)

            # Check for duplicates if context doesn't allow duplicates
            if "ctx_ids" in locals() and ctx_ids:
                # Check each context
                for context_id in ctx_ids:
                    if context_id is not None:
                        ctx_obj = (
                            context_dao.session.query(Context)
                            .filter_by(id=context_id)
                            .first()
                        )
                        if ctx_obj and not ctx_obj.allow_duplicates:
                            # Check each log ID for duplicates
                            for log_id in body.ids:
                                duplicate = context_dao.check_for_duplicates(
                                    context_id,
                                    log_id,
                                )
                                if duplicate:
                                    raise HTTPException(
                                        status_code=400,
                                        detail=f"Update would create a duplicate in context '{ctx_obj.name}' which doesn't allow duplicates. Log ID: {log_id}",
                                    )
            elif ctx_id is not None:
                # Single context case
                ctx_obj = (
                    context_dao.session.query(Context).filter_by(id=ctx_id).first()
                )
                if ctx_obj and not ctx_obj.allow_duplicates:
                    # Check each log ID for duplicates
                    for log_id in body.ids:
                        duplicate = context_dao.check_for_duplicates(ctx_id, log_id)
                        if duplicate:
                            raise HTTPException(
                                status_code=400,
                                detail=f"Update would create a duplicate in context '{ctx_obj.name}' which doesn't allow duplicates. Log ID: {log_id}",
                            )
        except ValueError as e:
            raise HTTPException(
                status_code=400,
                detail=f"Found differing log param value with the same version: {str(e)}",
            )
        except OverwriteError as e:
            raise HTTPException(
                status_code=400,
                detail=f"Existing value cannot be overwritten because overwrite is set to False: {str(e)}",
            )
        except ImmutableFieldError as e:
            raise HTTPException(
                status_code=400,
                detail=f"Field is immutable and cannot be modified: {str(e)}",
            )

    # Update context version if needed
    if "ctx_ids" in locals() and ctx_ids:
        # Handle multiple contexts if we have a list
        for context_id in ctx_ids:
            if context_id is not None:
                ctx_obj = (
                    context_dao.session.query(Context).filter_by(id=context_id).first()
                )
                if ctx_obj and ctx_obj.is_versioned and updates_by_log_id:
                    # Generate a summary of updated logs
                    log_count = len(updates_by_log_id)
                    update_desc = f"Updated {log_count} logs"

                    # archive state once and increment version
                    context_dao.archive_context_state(
                        ctx_obj,
                        name="update",
                        description=update_desc,
                    )
                    ctx_obj.version += 1
                    ctx_obj.updated_at = datetime.now(timezone.utc)
        # Commit all changes at once
        if updates_by_log_id:
            context_dao.session.commit()
    elif ctx_id is not None:
        # Original single context behavior
        ctx_obj = context_dao.session.query(Context).filter_by(id=ctx_id).first()
        if ctx_obj and ctx_obj.is_versioned and updates_by_log_id:
            # Generate a summary of updated logs
            log_count = len(updates_by_log_id)
            update_desc = f"Updated {log_count} logs"

            # archive state once and increment version
            context_dao.archive_context_state(
                ctx_obj,
                name="update",
                description=update_desc,
            )
            ctx_obj.version += 1
            ctx_obj.updated_at = datetime.now(timezone.utc)
            context_dao.session.commit()

    # Recompute derived logs that reference any updated base logs.
    if updated_ids:
        try:
            event_ids = [value for (_, value) in updated_ids]
            derived_logs_to_recompute = (
                session.query(DerivedLog)
                .join(LogEvent, LogEvent.id == DerivedLog.log_event_id)
                .filter(
                    LogEvent.project_id == project_id,
                    DerivedLog.log_event_id.in_(event_ids),
                )
                .all()
            )
            if derived_logs_to_recompute:
                derived_log_dao.recompute_derived_logs(
                    logs_to_recompute=derived_logs_to_recompute,
                    session=session,
                    json_encoder=CustomEncoder,
                )
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"Error recomputing derived logs for project id {project_id}: {e}",
            )

    return {"info": "Logs updated successfully!"}


@router.delete(
    "/logs",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {
                        "info": "Log entries deleted successfully!",
                    },
                },
            },
        },
        404: {
            "description": "Log Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "One or more logs were not found or you don't have permission to delete them.",
                    },
                },
            },
        },
    },
)
def delete_logs(
    request_fastapi: Request,
    body: DeleteLogEntryRequest,
    delete_empty_logs: bool = Query(
        default=False,
        description="Whether to delete logs which end up being empty as a result of "
        "the field deletion.",
        example=True,
    ),
    log_event_dao: LogEventDAO = Depends(),
    context_dao: ContextDAO = Depends(),
    project_dao: ProjectDAO = Depends(),
    field_type_dao: FieldTypeDAO = Depends(),
    session=Depends(get_db_session),
):
    """
    Deletes log entries based on specified criteria. Can delete both base logs and derived logs.

    If a context is provided, logs will be removed from that context instead of being entirely
    deleted, unless it is the last context associated with the log. This allows logs to be
    shared across multiple contexts and only removed from specific contexts when needed.

    Args:
        source_type: Controls which type of logs to delete:
            - 'all': Delete both base and derived logs (default)
            - 'base': Only delete base logs
            - 'derived': Only delete derived logs
    """
    if body.source_type not in ("all", "base", "derived"):
        raise HTTPException(
            status_code=400,
            detail="source_type must be one of: 'all', 'base', 'derived'",
        )

    not_found_logs = []
    not_found_entries = []
    ids_and_fields = _flatten_fields(body.ids_and_fields)
    deleted_fields = set()  # Track which fields were deleted for cascading deletion

    # Validate project existence
    user_id = request_fastapi.state.user_id
    try:
        project_id = project_dao.get_by_user_and_name(
            user_id=user_id,
            name=body.project,
        ).id
    except (IndexError, AttributeError):
        raise HTTPException(
            status_code=404,
            detail=f"Project '{body.project}' not found.",
        )

    # Validate context
    context_name = body.context if body.context else ""
    context = context_dao.filter(project_id=project_id, name=context_name)
    if not context:
        raise HTTPException(
            status_code=404,
            detail=f"Context '{context_name}' not found for project '{body.project}'.",
        )
    context_id = context[0][0].id

    # Track if we need to update the versioned context
    context_obj = None
    context_updated = False
    context_description = []

    # Group 1: Global field deletions (log_id is None)
    global_field_deletions = {k: v for k, v in ids_and_fields.items() if k is None}
    for log_id, fields in global_field_deletions.items():
        if len(fields) == 0:
            raise HTTPException(
                status_code=400,
                detail="Cannot delete all logs without specifying fields.",
            )

        # Get all log events for this project
        all_log_events_subq = select(
            session.query(LogEvent.id)
            .filter(LogEvent.project_id == project_id)
            .subquery(name="all_log_events"),
        )

        # Add fields to the deleted_fields set
        deleted_fields.update(fields)

        # Bulk delete from base logs with a single query
        if body.source_type in ("all", "base"):
            # Use a single DELETE statement for all fields
            deleted_count = (
                session.query(Log)
                .filter(Log.log_event_id.in_(all_log_events_subq), Log.key.in_(fields))
                .delete(synchronize_session=False)
            )
            if deleted_count > 0:
                context_description.append(
                    f"Deleted {len(fields)} fields from {deleted_count} base logs",
                )

        # Bulk delete from derived logs with a single query
        if body.source_type in ("all", "derived"):
            # Use a single DELETE statement for all fields
            deleted_count = (
                session.query(DerivedLog)
                .filter(
                    DerivedLog.log_event_id.in_(all_log_events_subq),
                    DerivedLog.key.in_(fields),
                )
                .delete(synchronize_session=False)
            )
            if deleted_count > 0:
                context_description.append(
                    f"Deleted {len(fields)} fields from {deleted_count} derived logs",
                )

        # Mark that we need to update the context
        if context_description:
            context_updated = True

    # Group 2: Entire log event deletions (fields is empty)
    entire_log_deletions = []
    for log_id, fields in ids_and_fields.items():
        if log_id is not None and len(fields) == 0:
            # Verify if the log belongs to the user
            try:
                if log_event_dao.get_user_id(id=log_id) != user_id:
                    raise IndexError
                entire_log_deletions.append(log_id)
            except IndexError:
                not_found_logs.append(log_id)

    if entire_log_deletions:
        if body.source_type == "derived":
            raise HTTPException(
                status_code=400,
                detail="Cannot delete derived logs without specifying fields.",
            )

        # Check which logs exist in other contexts
        logs_in_other_contexts = []
        logs_to_delete = []

        for log_id in entire_log_deletions:
            # Check if this log exists in any other context
            other_contexts = (
                session.query(LogEventContext.context_id)
                .filter(
                    LogEventContext.log_event_id == log_id,
                    LogEventContext.context_id != context_id,
                )
                .all()
            )

            if other_contexts:
                # Log exists in other contexts, just remove from this context
                logs_in_other_contexts.append(log_id)
            else:
                # Log doesn't exist in other contexts, delete it entirely
                logs_to_delete.append(log_id)

        # Remove logs from this context only
        if logs_in_other_contexts:
            removed_count = (
                session.query(LogEventContext)
                .filter(
                    LogEventContext.log_event_id.in_(logs_in_other_contexts),
                    LogEventContext.context_id == context_id,
                )
                .delete(synchronize_session=False)
            )
            if removed_count > 0:
                context_description.append(
                    f"Removed {removed_count} log events from context '{context_name}'",
                )
                context_updated = True

        # Delete logs that don't exist in other contexts
        if logs_to_delete:
            deleted_count = (
                session.query(LogEvent)
                .filter(LogEvent.id.in_(logs_to_delete))
                .delete(synchronize_session=False)
            )
            if deleted_count > 0:
                context_description.append(
                    f"Deleted {deleted_count} log events completely",
                )
                context_updated = True

    # Group 3: Partial field deletions (specific fields for specific log events)
    partial_deletions = {
        k: v for k, v in ids_and_fields.items() if k is not None and len(v) > 0
    }

    # Collect all log_event_id, field pairs for bulk deletion
    base_log_deletions = []
    derived_log_deletions = []
    potential_empty_logs = []

    for log_id, fields in partial_deletions.items():
        # Verify if the log belongs to the user
        try:
            if log_event_dao.get_user_id(id=log_id) != user_id:
                raise IndexError
        except IndexError:
            not_found_logs.append(log_id)
            continue

        # Add to potential empty logs list for later checking
        potential_empty_logs.append(log_id)

        # Add fields to the deleted_fields set
        deleted_fields.update(fields)

        # Add all field/log_id combinations directly without querying
        for field in fields:
            if body.source_type in ("all", "base"):
                base_log_deletions.append((log_id, field))
            if body.source_type in ("all", "derived"):
                derived_log_deletions.append((log_id, field))

        # Mark that we need to update the context
        context_updated = True
        context_description.append(f"Deleted fields from log_event_id={log_id}")

    # Perform bulk deletions for base logs
    if base_log_deletions and body.source_type in ("all", "base"):
        # Group by key for more efficient deletion
        key_to_event_ids = defaultdict(list)
        for event_id, key in base_log_deletions:
            key_to_event_ids[key].append(event_id)

        for key, event_ids in key_to_event_ids.items():
            try:
                deleted_count = (
                    session.query(Log)
                    .filter(
                        Log.key == key,
                        Log.log_event_id.in_(event_ids),
                    )
                    .delete(synchronize_session=False)
                )
            except:
                not_found_entries.append((event_ids, key))
                continue

            if deleted_count > 0:
                context_description.append(
                    f"Deleted field '{key}' from {deleted_count} base logs",
                )

    # Perform bulk deletions for derived logs
    if derived_log_deletions and body.source_type in ("all", "derived"):
        # Group by key for more efficient deletion
        key_to_event_ids = defaultdict(list)
        for event_id, key in derived_log_deletions:
            key_to_event_ids[key].append(event_id)

        for key, event_ids in key_to_event_ids.items():
            try:
                deleted_count = (
                    session.query(DerivedLog)
                    .filter(
                        DerivedLog.key == key,
                        DerivedLog.log_event_id.in_(event_ids),
                    )
                    .delete(synchronize_session=False)
                )
            except:
                not_found_entries.append((event_ids, key))
                continue

            if deleted_count > 0:
                context_description.append(
                    f"Deleted field '{key}' from {deleted_count} derived logs",
                )

    # Delete empty log events if requested
    if delete_empty_logs and potential_empty_logs:
        # Get all log_event_ids that still have logs in a single query
        still_used_base_ids = set(
            row[0]
            for row in session.query(Log.log_event_id)
            .filter(Log.log_event_id.in_(potential_empty_logs))
            .distinct()
        )

        still_used_derived_ids = set(
            row[0]
            for row in session.query(DerivedLog.log_event_id)
            .filter(DerivedLog.log_event_id.in_(potential_empty_logs))
            .distinct()
        )

        # Combine both sets
        still_used_ids = still_used_base_ids.union(still_used_derived_ids)

        # Find truly empty log events
        empty_log_ids = set(potential_empty_logs) - still_used_ids

        # For empty logs, check which ones exist in other contexts
        if empty_log_ids:
            logs_in_other_contexts = []
            logs_to_delete = []

            for log_id in empty_log_ids:
                # Check if this log exists in any other context
                other_contexts = (
                    session.query(LogEventContext.context_id)
                    .filter(
                        LogEventContext.log_event_id == log_id,
                        LogEventContext.context_id != context_id,
                    )
                    .all()
                )

                if other_contexts:
                    # Log exists in other contexts, just remove from this context
                    logs_in_other_contexts.append(log_id)
                else:
                    # Log doesn't exist in other contexts, delete it entirely
                    logs_to_delete.append(log_id)

            # Remove logs from this context only
            if logs_in_other_contexts:
                removed_count = (
                    session.query(LogEventContext)
                    .filter(
                        LogEventContext.log_event_id.in_(logs_in_other_contexts),
                        LogEventContext.context_id == context_id,
                    )
                    .delete(synchronize_session=False)
                )
                if removed_count > 0:
                    context_description.append(
                        f"Removed {removed_count} empty log events from context '{context_name}'",
                    )
                    context_updated = True

            # Delete logs that don't exist in other contexts
            if logs_to_delete:
                deleted_count = (
                    session.query(LogEvent)
                    .filter(LogEvent.id.in_(logs_to_delete))
                    .delete(synchronize_session=False)
                )
                if deleted_count > 0:
                    context_description.append(
                        f"Deleted {deleted_count} empty log events completely",
                    )
                    context_updated = True

    # Handle versioned contexts - do this only once after all deletions
    if context_updated and context_id:
        context_obj = (
            context_dao.session.query(Context).filter_by(id=context_id).first()
        )
        if context_obj and context_obj.is_versioned:
            context_dao.archive_context_state(
                context_obj,
                name="delete",
                description="; ".join(context_description),
            )
            context_obj.version += 1
            context_obj.updated_at = datetime.now(timezone.utc)

    # Handle cases where some logs or entries were not found
    if not_found_logs:
        raise HTTPException(
            status_code=404,
            detail=f"Logs with ids {not_found_logs} not found or you don't have permission to delete them.",
        )

    if not_found_entries:
        raise HTTPException(
            status_code=404,
            detail=f"Specified fields not found in logs with ids {not_found_entries}.",
        )

    # Cascading deletion: check if any deleted fields no longer exist in any logs
    if deleted_fields:
        # Get all fields that still exist in any logs with two efficient queries
        existing_base_fields = (
            session.query(Log.key)
            .join(LogEvent, LogEvent.id == Log.log_event_id)
            .filter(LogEvent.project_id == project_id)
            .distinct()
            .all()
        )
        existing_derived_fields = (
            session.query(DerivedLog.key)
            .join(LogEvent, LogEvent.id == DerivedLog.log_event_id)
            .filter(LogEvent.project_id == project_id)
            .distinct()
            .all()
        )

        # Combine all existing fields in one set operation
        all_existing_fields = set(
            [f[0] for f in existing_base_fields + existing_derived_fields],
        )

        # Find fields that no longer exist with a set difference operation
        fields_to_delete = deleted_fields - all_existing_fields

        # Bulk delete field types that are no longer used
        if fields_to_delete:
            for field in fields_to_delete:
                try:
                    field_type_dao.delete_field_type(
                        project_id=project_id,
                        field_name=field,
                        context_id=context_id,
                    )
                except Exception as e:
                    raise HTTPException(
                        status_code=500,
                        detail=f"Error deleting field type {field}: {str(e)}",
                    )

    return {"info": "Logs and fields deleted successfully!"}


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
    return_versions: bool = False,
):
    """
    Returns a combined list of base logs (Log) and derived logs (DerivedLog)
    that match the given user filters.  Each returned row is a tuple of
    (Log|DerivedLog ORM object, created_at (datetime), log_event_id (int)).

    Args:
        request_fastapi: The FastAPI request object.
        project: Name of the project to fetch logs from.
        column_context: String prefix to filter Log.key or DerivedLog.key.
            Also can specify 'params' or 'entries' to exclude the other.
        context: If provided, we join LogEventContext to filter on a
            "static" context row in the Context table.
        filter_expr: Optional string expression to filter based on fields
            (converted to SQL).
        sorting: JSON string specifying sorting criteria, e.g.
            '{"score":"ascending","timestamp":"descending"}'.
        from_ids: Optional string with '&'-separated log_event_ids to *include*.
        exclude_ids: Optional string with '&'-separated log_event_ids to *exclude*.
        from_fields: Optional string with '&'-separated field keys to *include*.
        exclude_fields: Optional string with '&'-separated field keys to *exclude*.
        limit: Max number of distinct log_event_ids to return (pagination).
        offset: Skip the first N distinct log_event_ids (pagination).
        project_dao, field_type_dao, context_dao: DAO objects for DB logic.
        session: The SQLAlchemy session dependency.
        latest_timestamp: If True, returns only the latest .updated_at timestamp
            (as an ISO string) across all matching logs, otherwise returns
            the matching rows.
        return_versions: If True, return all versions of logs. Only valid for versioned contexts.
        version: If provided, return only the logs with the specified version from LogHistory.
        tuple: (list_of_rows, context_len, total_count)
            Where:
                list_of_rows = [(Log|DerivedLog, created_at, log_event_id), ...]
                context_len = length of the column_context prefix that was stripped
                              from the final keys (for your reference)
                total_count = total number of distinct log_event_ids before pagination

            Or, if latest_timestamp=True, it returns the single latest timestamp
            as a string (or None if none found).
    """
    user_id = request_fastapi.state.user_id

    # 1) Validate the project
    try:
        project_id = project_dao.get_by_user_and_name(name=project, user_id=user_id).id
    except (IndexError, AttributeError):
        raise not_found(f"Project {project}")

    # 2) Build initial query for relevant LogEvent rows
    #    (filter_expr, from_ids, exclude_ids, plus optional static context)
    log_event_query = session.query(LogEvent.id).filter(
        LogEvent.project_id == project_id,
    )
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
            # Only allow 'exists' checks for image fields
            def validate_filter_dict(fd):
                if isinstance(fd, dict):
                    if "type" in fd and fd["type"] == "identifier":
                        field = fd.get("value")
                        if is_image_field(field, field_types):
                            parent = getattr(validate_filter_dict, "parent", None)
                            if parent and parent.get("operand") not in (
                                "exists",
                                "isNone",
                            ):
                                raise HTTPException(
                                    status_code=400,
                                    detail=f"Field '{field}' is an image type and can only be used with 'exists' operator",
                                )
                    for k, v in fd.items():
                        if isinstance(v, dict):
                            validate_filter_dict.parent = fd
                            validate_filter_dict(v)

            validate_filter_dict(filter_dict)
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
                # Normal SQL expression
                log_event_query = log_event_query.filter(condition)

    # filter LogEvent by "static context" (LogEventContext + Context)
    if context:
        # Get context object and check if it's versioned when return_versions=True
        context_obj = context_dao.filter(name=context, project_id=project_id)
    else:
        # use the default context
        context_obj = context_dao.filter(name="", project_id=project_id)
        if not context_obj:
            # no logs present within this context, return empty logs
            return [], 0, 0

    if not context_obj:
        raise HTTPException(
            status_code=404,
            detail=f"Context '{context}' not found",
        )
    context_obj = context_obj[0][0]
    ctx_id_val = context_obj.id

    # If return_versions is True, verify the context is versioned
    if return_versions and not context_obj.is_versioned:
        raise HTTPException(
            status_code=400,
            detail="Cannot return versions for unversioned context",
        )

    # Filter by context_id
    log_event_query = log_event_query.filter(
        exists(
            select(1)
            .select_from(LogEventContext)
            .where(
                and_(
                    LogEventContext.log_event_id == LogEvent.id,
                    LogEventContext.context_id == ctx_id_val,
                ),
            ),
        ),
    )

    # Turn into a subquery => these are the log_event_ids we care about so far
    relevant_log_events = log_event_query.subquery(name="relevant_log_events")

    # 3) Union base logs and derived logs into a single subquery
    #    so they can be treated identically downstream.
    unified_logs_subq = _build_unified_logs_subquery(
        session=session,
        relevant_log_events=relevant_log_events,
        return_versions=return_versions,
    )

    # 4) Apply "column_context" + 'params'/'entries' logic
    #    We parse the user-supplied column_context (slash-separated).
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
        # Rebuild the actual context prefix (excluding the 'entries'/'params' tokens)
        column_context = "/".join(
            [substr for substr in split_context if substr not in ("params", "entries")],
        )
        if column_context:
            # Ensure trailing slash
            if column_context[-1] != "/":
                column_context += "/"
            context_len = len(column_context)

    filtered_logs_q = session.query(unified_logs_subq).filter(
        True,
    )  # start with everything

    # If we have a column_context prefix, we do .where(key.startswith(...))
    if column_context:
        filtered_logs_q = filtered_logs_q.filter(
            unified_logs_subq.c.key.startswith(column_context),
        )

    # Handle from_ids vs exclude_ids
    if from_ids and exclude_ids:
        raise HTTPException(
            status_code=400,
            detail="Cannot set both from_ids and exclude_ids.",
        )

    # Handle ID filtering differently based on return_versions
    if return_versions:
        if from_ids:
            try:
                # Validate from_ids format for versioned logs
                from_ids = json.loads(from_ids)
                if not isinstance(from_ids, list):
                    raise ValueError(
                        "from_ids must be a list when return_versions is True",
                    )
                for item in from_ids:
                    if (
                        not isinstance(item, dict)
                        or "id" not in item
                        or "version" not in item
                    ):
                        raise ValueError(
                            "Each item in from_ids must have 'id' and 'version' keys",
                        )
                allowed_pairs = [(item["id"], item["version"]) for item in from_ids]
                # Apply filtering at the Log/LogHistory level since we need version info
                filtered_logs_q = filtered_logs_q.filter(
                    tuple_(
                        unified_logs_subq.c.log_event_id,
                        unified_logs_subq.c.context_version,
                    ).in_(allowed_pairs),
                )
            except ValueError as e:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid from_ids format for versioned logs: {str(e)}",
                )
        if exclude_ids:
            try:
                # Validate exclude_ids format for versioned logs
                exclude_ids = json.loads(exclude_ids)
                if not isinstance(exclude_ids, list):
                    raise ValueError(
                        "exclude_ids must be a list when return_versions is True",
                    )
                for item in exclude_ids:
                    if (
                        not isinstance(item, dict)
                        or "id" not in item
                        or "version" not in item
                    ):
                        raise ValueError(
                            "Each item in exclude_ids must have 'id' and 'version' keys",
                        )
                excluded_pairs = [(item["id"], item["version"]) for item in exclude_ids]
                # Apply filtering at the Log/LogHistory level since we need version info
                filtered_logs_q = filtered_logs_q.filter(
                    ~tuple_(
                        unified_logs_subq.c.log_event_id,
                        unified_logs_subq.c.context_version,
                    ).in_(excluded_pairs),
                )
            except ValueError as e:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid exclude_ids format for versioned logs: {str(e)}",
                )
    else:
        # For non-versioned queries, use simple log_event_id filtering
        if from_ids:
            include_ids = [int(x) for x in from_ids.split("&")]
            filtered_logs_q = filtered_logs_q.filter(
                unified_logs_subq.c.log_event_id.in_(include_ids),
            )
        elif exclude_ids:
            exclude_set = [int(x) for x in exclude_ids.split("&")]
            filtered_logs_q = filtered_logs_q.filter(
                unified_logs_subq.c.log_event_id.notin_(exclude_set),
            )

    # If exclude_params / exclude_entries => filter on version
    # TODO(yusha): handle filtering out corresponding rows from LogHistory as well
    if exclude_params:
        filtered_logs_q = filtered_logs_q.filter(
            unified_logs_subq.c.param_version.is_(None),
        )
    elif exclude_entries:
        filtered_logs_q = filtered_logs_q.filter(
            unified_logs_subq.c.param_version.isnot(None),
        )

    # 5) from_fields / exclude_fields
    if from_fields and exclude_fields:
        raise HTTPException(
            status_code=400,
            detail="Only one of from_fields or exclude_fields can be set.",
        )

    if from_fields:
        allowed_fields = from_fields.split("&")
        filtered_logs_q = filtered_logs_q.filter(
            unified_logs_subq.c.key.in_(allowed_fields),
        )
    elif exclude_fields:
        excluded_fields = exclude_fields.split("&")
        filtered_logs_q = filtered_logs_q.filter(
            unified_logs_subq.c.key.notin_(excluded_fields),
        )

    # now we have a single table of
    # (id, log_event_id, key, value, inferred_type, version, updated_at, created_at, source_type)
    filtered_logs_subq = filtered_logs_q.subquery(name="filtered_logs_subq")

    # 6) Find the distinct log_event_ids that actually remain
    distinct_ids_subq = (
        session.query(filtered_logs_subq.c.log_event_id.label("log_event_id"))
        .distinct(filtered_logs_subq.c.log_event_id)
        .subquery(name="distinct_ids_subq")
    )

    # 7) Sorting logic
    sorted_query = session.query(distinct_ids_subq.c.log_event_id)

    sort_criteria = []

    if sorting:
        # e.g. sorting='{"score":"ascending","timestamp":"descending"}'
        sort_dict = json.loads(sorting)

        # For each field in sort_dict, we outer-join a subquery from filtered_logs_subq
        # that picks out the relevant value for that field. Then we cast it if known.
        for sort_key, mode in sort_dict.items():
            # Skip image fields from sorting
            if is_image_field(sort_key, field_types):
                continue
            if mode not in ("ascending", "descending"):
                raise HTTPException(
                    status_code=400,
                    detail=f"Sort mode must be 'ascending' or 'descending', got {mode}.",
                )

            # Build a subquery => (log_event_id, value)
            # so we can outerjoin to it
            key_subq = (
                session.query(
                    filtered_logs_subq.c.log_event_id.label("log_event_id"),
                    filtered_logs_subq.c.value.label("raw_value"),
                )
                .filter(filtered_logs_subq.c.key == sort_key)
                .subquery(name=f"sort_{sort_key}_subq")
            )

            # Outerjoin
            sorted_query = sorted_query.outerjoin(
                key_subq,
                key_subq.c.log_event_id == distinct_ids_subq.c.log_event_id,
            )

            # If recognized type => cast
            if sort_key in field_types:
                python_type = field_types[sort_key]
                cast_type = STR_TO_SQL_TYPES.get(python_type, None)
                # Now build an expression for sorting
                sort_expr = (
                    cast(
                        cast(key_subq.c.raw_value, String),
                        cast_type,
                    )
                    if cast_type in (DateTime, Date, Time, Interval)
                    else cast(key_subq.c.raw_value, cast_type)
                )
            else:
                sort_expr = key_subq.c.raw_value

            direction = asc if mode == "ascending" else desc
            sort_criteria.append(direction(sort_expr).nulls_last())

    # Always fallback to sorting by log_event_id desc if not explicitly specified
    if not sorting or "id" not in sorting:
        sort_criteria.append(distinct_ids_subq.c.log_event_id.desc())

    sorted_query = sorted_query.add_columns(
        func.row_number().over(order_by=sort_criteria).label("row_num"),
    ).order_by("row_num")

    # 8) Pagination
    count = sorted_query.count()  # total distinct log_event_ids
    if limit:
        sorted_query = sorted_query.limit(limit)
    if offset:
        sorted_query = sorted_query.offset(offset)

    paginated_ids_subq = sorted_query.subquery(name="paginated_ids_subq")

    # 9) If user just wants the latest timestamp
    if latest_timestamp:
        # find the max(updated_at) among logs that match the final set of log_event_ids
        max_updated_at = (
            session.query(func.max(filtered_logs_subq.c.updated_at))
            .join(
                paginated_ids_subq,
                paginated_ids_subq.c.log_event_id == filtered_logs_subq.c.log_event_id,
            )
            .scalar()
        )
        return max_updated_at.isoformat() if max_updated_at else None

    # 10) Otherwise, fetch final rows => join to paginated_ids_subq
    #     so we only get logs in the final log_event_id set, in sorted order.
    raw_rows = _get_final_logs(session, filtered_logs_subq, paginated_ids_subq)
    # raw_rows is a list of:
    # [
    #   (
    #       id, log_event_id, key, value, inferred_type, param_version, context_version,
    #       created_at, source_type
    #   ), ...
    # ]

    # 11) Return the raw rows so that the top-level get_logs can do the final formatting.
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

    # 12) Return results
    return results, context_len, count


@router.get(
    "/logs",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {
                        "params": {},
                        "logs": [
                            {
                                "id": "0",
                                "ts": "2024-10-30 12:20:03",
                                "entries": {
                                    "key1": "a",
                                    "key2": 1.0,
                                },
                                "derived_entries": {},
                                "params": {},
                            },
                            {
                                "id": "1",
                                "ts": "2024-10-30 12:22:14",
                                "entries": {
                                    "key1": "b",
                                    "key2": 2.0,
                                },
                                "derived_entries": {},
                                "params": {},
                            },
                        ],
                        "count": 2,
                    },
                },
            },
        },
        404: {
            "description": "Project Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Project <project> not found.",
                    },
                },
            },
        },
    },
)
def get_logs(
    request_fastapi: Request,
    project: str = Query(
        description="Name of the project to get entries from.",
        example="eval-project",
    ),
    column_context: Optional[str] = Query(
        None,
        description="The context (prepending '/' seperated field names) from which to retrieve the logs.",
        example="subjects/science/physics",
    ),
    context: Optional[str] = Query(
        None,
        description="Static context to filter logs by.",
        example="training",
    ),
    return_versions: bool = Query(
        False,
        description="Whether to return all versions of logs. Only valid for versioned contexts.",
    ),
    group_threshold: Optional[int] = Query(
        None,
        description="When set, entries that appear in at least this many logs will be grouped together.",
    ),
    value_limit: Optional[int] = Query(
        None,
        description="Maximum number of characters to return for string values.",
    ),
    filter_expr: Optional[str] = Query(
        None,
        description="Boolean string to filter entries. TODO: Detailed page.",
        example="len(output) > 200 and temperature == 0.5",
    ),
    sorting: Optional[str] = Query(
        None,
        description="Dict with fields as keys and either 'ascending' or 'descending' as values. The first entry in the dict is the last field to be sorted by, which takes ultimate precedent, with other keys only remaining in order when the first key values are equal.",
        example={"score": "ascending", "timestamp": "descending"},
    ),
    group_sorting: Optional[str] = Query(
        None,
        description="Sorting configuration for groups when using group_by. Specifies how to sort groups relative to each other based on aggregated metrics.",
        example={
            "entries/student": {
                "field": "score",
                "direction": "descending",
                "metric": "mean",
            },
        },
    ),
    from_ids: Optional[Any] = Query(
        None,
        description="The log ids which are permitted to be included in the search. Each log id listed does not need to be returned, but no logs which are not included in this list can be returned. This argument *cannot* be set if `exclude_ids` is set.",
        example="0&1&2",
    ),
    exclude_ids: Optional[Any] = Query(
        None,
        description="The log ids which cannot be returned from the search. None of the listed ids will be returned, even if the logs are valid as per the filtering expression etc. This argument *cannot* be set if `from_ids` is set.",
        example="0&1&2",
    ),
    from_fields: Optional[str] = Query(
        None,
        description="The fields which are permitted to be included in the search. Each field listed does not need to be returned, but no fields which are not included in this list can be returned. This argument *cannot* be set if `exclude_fields` is set.",
        example="score&response",
    ),
    exclude_fields: Optional[str] = Query(
        None,
        description="The fields which cannot be returned from the search. None of the listed fields will be returned, even if the fields are valid as per the filtering expression etc. This argument *cannot* be set if `from_fields` is set.",
        example="score&response",
    ),
    limit: Optional[int] = Query(None, ge=1, le=200),
    offset: int = Query(0, ge=0),
    group_by: Optional[List[str]] = Query(
        None,
        description="List of fields to group results by. Results will be nested based on these fields.",
        example=["model", "temperature"],
    ),
    group_limit: Optional[int] = Query(
        None,
        description="Maximum number of groups to return at each level",
        ge=1,
    ),
    group_offset: int = Query(
        0,
        description="Number of groups to skip at each level",
        ge=0,
    ),
    group_depth: Optional[int] = Query(
        None,
        description="Maximum depth of nested groups to return. If not specified, all levels are returned.",
    ),
    nested_groups: bool = Query(
        True,
        description="If True, groups are returned as a nested structure; if False, groups are returned as flat per-field mappings.",
    ),
    groups_only: bool = Query(
        False,
        description="If True, do not include a full logs list; only return groups (with leaf values being either log ids or timestamps).",
    ),
    return_timestamps: bool = Query(
        False,
        description="When groups_only is True, return each leaf as a mapping from log id to timestamp instead of just a list of log ids.",
    ),
    return_ids_only: bool = False,
    project_dao: ProjectDAO = Depends(),
    field_type_dao: FieldTypeDAO = Depends(),
    context_dao: ContextDAO = Depends(),
    session=Depends(get_db_session),
):
    """
    Returns a list of filtered log entries from a project with various expressiveness options:

      1. **Monolithic mode** (when group_by is not provided):
         - Returns a flat list of log entries (with fields clipped if value_limit is set).
         - Optionally factors out repeated fields into a grouped_entries field if group_threshold is set.

      2. **Grouped mode** (when group_by is provided):
         - Supports multi-level grouping of logs. The order of fields in group_by dictates the nesting order.
         - Supports pagination at the group level using group_limit and group_offset.
         - Supports limiting the nesting depth with group_depth.
         - When nested_groups is True, returns a nested structure under the "logs" key.
         - When nested_groups is False, returns flat per-field mappings under the "groups" key.
         - When groups_only is True, the detailed log objects are omitted and leaves are simplified
           to either lists of log ids (if return_timestamps is False) or mappings of {log id: timestamp} (if True).

      3. **Return IDs only mode**:
         - If return_ids_only is True, returns only the log event ids.
         - If return_versions is also True, returns a list of objects with both id and version information.

    The response always includes:
      - `params`: The parameter versions used across the logs.
      - `count`: The total number of logs matching the query.
      - Additionally, it includes either `logs` (in monolithic or nested grouping mode) or `groups` (in flat grouping mode)
        as specified by the arguments.

    If return_versions=True:
    - Returns all versions of logs in versioned contexts
    - from_ids and exclude_ids must be provided as lists of objects with 'id' and 'version' keys
    - Each object must have format: {"id": log_event_id, "version": version_number}
    - This is only valid for logs in versioned contexts

    If return_versions=False (default):
    - Returns only the latest version of each log
    - from_ids and exclude_ids should be strings of '&'-separated log event IDs
    """
    try:
        project_id = project_dao.get_by_user_and_name(
            name=project,
            user_id=request_fastapi.state.user_id,
        ).id
    except Exception as e:
        raise HTTPException(
            status_code=404,
            detail=f"Project {project} not found.",
        )
    # Format logs into flat structure.
    context_name = "" if not context else context
    context_obj = context_dao.filter(name=context_name, project_id=project_id)
    if context_obj:
        context_id = context_obj[0][0].id
    else:
        context_id = None
    # -----------------------------------------------------------
    # Stage 1: Monolithic (non-grouped) Case
    # -----------------------------------------------------------
    if not group_by:
        all_rows, context_len, total_count = _get_logs_query(
            request_fastapi,
            project=project,
            column_context=column_context,
            context=context,
            filter_expr=filter_expr,
            sorting=sorting,
            from_ids=from_ids,
            exclude_ids=exclude_ids,
            from_fields=from_fields,
            exclude_fields=exclude_fields,
            limit=limit,
            offset=offset,
            project_dao=project_dao,
            field_type_dao=field_type_dao,
            context_dao=context_dao,
            session=session,
            return_versions=return_versions,
        )
        if return_ids_only:
            if return_versions:
                # Return list of objects with both id and version information
                id_version_map = {}
                for row in all_rows:
                    event_id = row[7]  # log_event_id
                    version = row[4]  # context_version
                    if event_id not in id_version_map:
                        id_version_map[event_id] = set()
                    if version is not None:
                        id_version_map[event_id].add(version)

                result = []
                for event_id, versions in id_version_map.items():
                    if versions:
                        for version in versions:
                            result.append({"id": event_id, "version": version})
                    else:
                        result.append({"id": event_id})
                return result
            return list(
                dict.fromkeys(row[7] for row in all_rows),
            )  # Return unique log_event_ids

        # Format logs into flat structure.
        field_order_map = field_type_dao.get_ordered_field_names(
            project_id,
            context_id=context_id,
        )
        logs_out, params_out = _format_flat_logs(
            all_rows,
            context_len,
            value_limit,
            field_order_map,
        )

        # Apply grouping of repeated fields if group_threshold is set.
        grouped_entries = {}
        if group_threshold is not None and group_threshold > 0:
            logs_out, grouped_entries = apply_group_threshold(logs_out, group_threshold)

        response = {
            "params": params_out,
            "logs": logs_out,
            "count": total_count,
        }
        if grouped_entries:
            response["grouped_entries"] = grouped_entries

        return response

    # -----------------------------------------------------------
    # Stage 2: Grouping Case
    #   (a) Retrieve all matching log event IDs (ignoring limit/offset)
    # -----------------------------------------------------------
    event_ids_subq, total_count = _get_all_filtered_log_event_ids(
        request_fastapi=request_fastapi,
        project=project,
        context=context,
        filter_expr=filter_expr,
        from_ids=from_ids,
        return_versions=return_versions,
        exclude_ids=exclude_ids,
        project_dao=project_dao,
        context_dao=context_dao,
        field_type_dao=field_type_dao,
        session=session,
        as_subquery=True,  # Keep IDs as a subquery to avoid materializing large lists
    )
    field_order_map = field_type_dao.get_ordered_field_names(
        project_id,
        context_id=context_id,
    )
    field_map = field_type_dao.get_field_types(
        project_id,
        context_id=context_id,
    )
    if return_ids_only:
        all_ids = session.query(event_ids_subq).all()  # each row is a tuple (id,)
        event_ids = [r[0] for r in all_ids]
        return list(dict.fromkeys(event_ids))

    # -----------------------------------------------------------
    # Stage 3: Get Parameter Versions for the Log Events
    # -----------------------------------------------------------
    params_out = _get_params_for_log_events(event_ids_subq, session)

    # -----------------------------------------------------------
    # Stage 4: Build Grouped Structure
    # -----------------------------------------------------------
    if nested_groups:
        grouped_result = _build_grouped_data(
            request_fastapi=request_fastapi,
            project_id=project_id,
            log_event_ids=event_ids_subq,
            field_order_map=field_order_map,
            field_types=field_map,
            group_by=group_by,
            group_depth=group_depth,
            group_limit=group_limit,
            group_offset=group_offset,
            group_sorting=group_sorting,
            level=0,
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
            return_versions=return_versions,
        )

        final_result = {
            "params": params_out,
            "logs": grouped_result,
            "count": total_count,
        }

    else:
        # -----------------------------------------------------------
        # Stage 4B: Flat Groups Mode for the View Pane.
        #   (a) Fetch flat logs.
        #   (b) Build per-field grouping structure.
        # -----------------------------------------------------------
        rows, context_len, _ = _fetch_logs_for_event_ids(
            request_fastapi=request_fastapi,
            event_ids=event_ids_subq,
            project_id=project_id,
            column_context=column_context,
            context=context,
            from_fields=from_fields,
            exclude_fields=exclude_fields,
            sorting=sorting,
            limit=limit,
            offset=offset,
            parent_fields="",
            project_dao=project_dao,
            field_type_dao=field_type_dao,
            context_dao=context_dao,
            return_versions=return_versions,
            session=session,
        )
        logs_out, _ = _format_flat_logs(rows, context_len, value_limit, field_order_map)

        groups = {}

        def parse_group_key(key: str) -> Tuple[str, str]:
            parts = key.split("/", 1)
            return (parts[0], parts[1]) if len(parts) == 2 else ("", key)

        for group_field in group_by:
            prefix, raw_key = parse_group_key(group_field)
            is_param = prefix == "params"
            distinct_values = _get_distinct_group_values(
                log_event_ids=event_ids_subq,
                group_key=raw_key,
                session=session,
                is_param=is_param,
            )
            value_to_ids = {}
            used_ids = set()
            for val in distinct_values:
                subset_ids = _get_log_event_ids_for_group_value(
                    log_event_ids=event_ids_subq,
                    group_key=raw_key,
                    group_value=val,
                    session=session,
                    is_param=is_param,
                )
                value_to_ids[val] = subset_ids
                used_ids.update(subset_ids)
            all_ids = session.query(event_ids_subq).all()
            event_ids = [r[0] for r in all_ids]
            missing_ids = list(set(event_ids) - used_ids)
            if missing_ids:
                value_to_ids["null"] = missing_ids

            all_keys = list(value_to_ids.keys())
            total_distinct = len(all_keys)
            all_keys_sorted = sorted(all_keys, key=lambda x: (x is None, x))
            if group_limit is not None:
                paged_keys = all_keys_sorted[group_offset : group_offset + group_limit]
            else:
                paged_keys = all_keys_sorted
            paged_mapping = {k: value_to_ids[k] for k in paged_keys}
            field_total = sum(len(ids) for ids in value_to_ids.values())
            groups[group_field] = {
                **paged_mapping,
                "group_count": total_distinct,
                "count": field_total,
            }

        final_result = {
            "params": params_out,
            "groups": groups,
            "logs": logs_out,
            "count": total_count,
        }

    # -----------------------------------------------------------
    # Stage 5: Simplify Leaves if groups_only is True
    #   (Convert full log objects to simplified leaf values.)
    # -----------------------------------------------------------
    if groups_only:

        def simplify_leaves(node):
            if isinstance(node, list):
                # Could be a list of log-dict(s) or a list of log-ids (ints)
                if not node:
                    return node
                first_item = node[0]
                if isinstance(first_item, dict):
                    if return_timestamps:
                        return {
                            str(log["id"]): log["ts"]
                            for log in node
                            if "id" in log and "ts" in log
                        }
                    else:
                        return [log["id"] for log in node if "id" in log]
                else:
                    return node
            elif isinstance(node, dict):
                new_node = {}
                for k, v in node.items():
                    if k in ("group_count", "count"):
                        new_node[k] = v
                    else:
                        new_node[k] = simplify_leaves(v)
                return new_node
            return node

        if nested_groups:
            final_result["logs"] = simplify_leaves(final_result["logs"])
        else:
            final_result.pop("logs", None)
            final_result["groups"] = simplify_leaves(final_result["groups"])

    # -----------------------------------------------------------
    # Stage 6: Return the Final Result.
    # -----------------------------------------------------------
    return final_result


@router.get(
    "/logs/latest_timestamp",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {
                        "params": {},
                        "logs": [
                            {
                                "id": "0",
                                "ts": "2024-10-30 12:20:03",
                                "entries": {
                                    "key1": "a",
                                    "key2": 1.0,
                                },
                                "params": {},
                            },
                            {
                                "id": "1",
                                "ts": "2024-10-30 12:22:14",
                                "entries": {
                                    "key1": "b",
                                    "key2": 2.0,
                                },
                                "params": {},
                            },
                        ],
                        "count": 2,
                    },
                },
            },
        },
        404: {
            "description": "Project Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Project <project> not found.",
                    },
                },
            },
        },
    },
)
def get_logs_latest_timestamp(
    request_fastapi: Request,
    project: str = Query(
        description="Name of the project to get entries from.",
        example="eval-project",
    ),
    column_context: Optional[str] = Query(
        None,
        description="The context (prepending '/' seperated field names) from which to "
        "retrieve the logs.",
        example="subjects/science/physics",
    ),
    context: Optional[str] = Query(
        None,
        description="Static context to filter logs by.",
        example="training",
    ),
    filter_expr: Optional[str] = Query(
        None,
        description="Boolean string to filter entries. TODO: Detailed page.",
        example="len(output) > 200 and temperature == 0.5",
    ),
    sorting: Optional[str] = Query(
        None,
        description="Dict with fields as keys and either 'ascending' or 'descending' "
        "as values. The first entry in the dict is the last field to be "
        "sorted by, which takes ultimate precedent, with other keys only "
        "remaining in order when the first key values are equal.",
        example={"score": "ascending", "timestamp": "descending"},
    ),
    from_ids: Optional[str] = Query(
        None,
        description="The log ids which are permitted to be included in the search. "
        "Each log id listed does not need to be returned, but no logs "
        "which are not included in this list can be returned. This "
        "argument *cannot* be set if `exclude_ids` is set.",
        example="0&1&2",
    ),
    exclude_ids: Optional[str] = Query(
        None,
        description="The log ids which cannot be returned from the search. "
        "None of the listed ids will be returned, even if the logs are "
        "valid as per the filtering expression etc. This argument *cannot* "
        "be set if `from_ids` is set.",
        example="0&1&2",
    ),
    from_fields: Optional[str] = Query(
        None,
        description="The fields which are permitted to be included in the search. "
        "Each field listed does not need to be returned, but no fields "
        "which are not included in this list can be returned. This "
        "argument *cannot* be set if `exclude_fields` is set.",
        example="score&response",
    ),
    exclude_fields: Optional[str] = Query(
        None,
        description="The fields which cannot be returned from the search. "
        "None of the listed fields will be returned, even if the fields "
        "are valid as per the filtering expression etc. This argument "
        "*cannot* be set if `from_fields` is set.",
        example="score&response",
    ),
    limit: Optional[int] = Query(None, ge=1, le=200),
    offset: int = Query(0, ge=0),
    project_dao: ProjectDAO = Depends(),
    field_type_dao: FieldTypeDAO = Depends(),
    context_dao: ContextDAO = Depends(),
    session=Depends(get_db_session),
):
    """
    Returns the update timestamp of the most recently updated log within the specified
    page and filter bounds.
    """
    return _get_logs_query(
        request_fastapi,
        project=project,
        column_context=column_context,
        context=context,
        filter_expr=filter_expr,
        sorting=sorting,
        from_ids=from_ids,
        exclude_ids=exclude_ids,
        from_fields=from_fields,
        exclude_fields=exclude_fields,
        limit=limit,
        offset=offset,
        project_dao=project_dao,
        field_type_dao=field_type_dao,
        context_dao=context_dao,
        session=session,
        latest_timestamp=True,
    )


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
        if field_type == "timestamp":
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
                return f"timestamp({value})"

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

    # 1) Build initial query to find matching LogEvent IDs
    query = session.query(LogEvent.id).filter(LogEvent.project_id == project_obj.id)

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
            Log.log_event_id.label("log_event_id"),
            Log.value.label("value"),
            Log.inferred_type.label("inferred_type"),
        )
        .filter(Log.key == key)
        .join(LogEvent, Log.log_event_id == LogEvent.id)
        .filter(LogEvent.project_id == project_obj.id)
    )

    agg_derived_q = (
        session.query(
            DerivedLog.log_event_id.label("log_event_id"),
            DerivedLog.value.label("value"),
            DerivedLog.inferred_type.label("inferred_type"),
        )
        .filter(DerivedLog.key == key)
        .join(LogEvent, DerivedLog.log_event_id == LogEvent.id)
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
                    Log.log_event_id.label("log_event_id"),
                    Log.version.label("value"),
                    literal("int").label("inferred_type"),
                )
                .filter(Log.key == group_field)
                .join(LogEvent, Log.log_event_id == LogEvent.id)
                .filter(LogEvent.project_id == project_obj.id)
            )
            group_subq = group_q.subquery(f"group_{idx}")
        else:
            # For non-parameters, union base logs and derived logs
            group_log_q = (
                session.query(
                    Log.log_event_id.label("log_event_id"),
                    Log.value.label("value"),
                    Log.inferred_type.label("inferred_type"),
                )
                .filter(Log.key == group_field)
                .join(LogEvent, Log.log_event_id == LogEvent.id)
                .filter(LogEvent.project_id == project_obj.id)
            )

            group_derived_q = (
                session.query(
                    DerivedLog.log_event_id.label("log_event_id"),
                    DerivedLog.value.label("value"),
                    DerivedLog.inferred_type.label("inferred_type"),
                )
                .filter(DerivedLog.key == group_field)
                .join(LogEvent, DerivedLog.log_event_id == LogEvent.id)
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

    # Cast expression for the aggregator value
    cast_expr = case(
        (
            X.c.inferred_type == "list",
            func.jsonb_array_length(cast(X.c.value, JSONB)).cast(Float),
        ),
        (
            X.c.inferred_type == "dict",
            select(func.count())
            .select_from(func.jsonb_object_keys(cast(X.c.value, JSONB)))
            .scalar_subquery()
            .cast(Float),
        ),
        (
            X.c.inferred_type == "bool",
            X.c.value.cast(BOOLEAN).cast(INTEGER).cast(Float),
        ),
        (
            X.c.inferred_type == "str",
            func.length(cast(X.c.value, JSONB)[0].astext).cast(Float),
        ),
        (
            X.c.inferred_type == "timestamp",
            func.extract("epoch", cast(cast(X.c.value, String), TIMESTAMP)).cast(Float),
        ),
        (
            X.c.inferred_type == "time",
            # Extract seconds using time-specific casting
            func.mod(
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
        (
            X.c.inferred_type == "date",
            # Extract epoch using date-specific casting
            func.extract(
                "epoch",
                func.cast(func.trim(func.cast(X.c.value, String), '"'), Date),
            ).cast(Float),
        ),
        (
            X.c.inferred_type == "timedelta",
            # Parse ISO 8601 duration format (e.g., "P1DT6H") to seconds
            # This extracts days, hours, minutes, seconds separately and converts to total seconds
            (
                # Days component (86400 seconds per day)
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
                +
                # Hours component (3600 seconds per hour)
                func.coalesce(
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
                +
                # Minutes component (60 seconds per minute)
                func.coalesce(
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
                +
                # Seconds component
                func.coalesce(
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
        (X.c.inferred_type == "float", X.c.value.cast(Float)),
        (X.c.inferred_type == "int", X.c.value.cast(Float)),
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
    # 1) Build initial query to find matching LogEvent IDs
    query = session.query(LogEvent.id).filter(LogEvent.project_id == project_obj.id)

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
            Log.log_event_id.label("log_event_id"),
            Log.value.label("value"),
            Log.inferred_type.label("inferred_type"),
        )
        .filter(Log.key == key)
        .join(LogEvent, Log.log_event_id == LogEvent.id)
        .filter(LogEvent.project_id == project_obj.id)
    )

    # Derived logs
    derived_q = (
        session.query(
            DerivedLog.log_event_id.label("log_event_id"),
            DerivedLog.value.label("value"),
            DerivedLog.inferred_type.label("inferred_type"),
        )
        .filter(DerivedLog.key == key)
        .join(LogEvent, DerivedLog.log_event_id == LogEvent.id)
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

    # interpret X.c.value depending on X.c.inferred_type.
    cast_expr = case(
        (
            X.c.inferred_type == "list",
            func.jsonb_array_length(cast(X.c.value, JSONB)).cast(Float),
        ),
        (
            X.c.inferred_type == "dict",
            select(func.count())
            .select_from(func.jsonb_object_keys(cast(X.c.value, JSONB)))
            .scalar_subquery()
            .cast(Float),
        ),
        (
            X.c.inferred_type == "bool",
            X.c.value.cast(BOOLEAN).cast(INTEGER).cast(Float),
        ),
        (
            X.c.inferred_type == "str",
            func.length(cast(X.c.value, JSONB)[0].astext).cast(Float),
        ),
        (
            X.c.inferred_type == "timestamp",
            func.extract("epoch", cast(cast(X.c.value, String), TIMESTAMP)).cast(Float),
        ),
        (
            X.c.inferred_type == "time",
            # Extract seconds using time-specific casting
            func.mod(
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
        (
            X.c.inferred_type == "date",
            # Extract epoch using date-specific casting
            func.extract(
                "epoch",
                func.cast(func.trim(func.cast(X.c.value, String), '"'), Date),
            ).cast(Float),
        ),
        (
            X.c.inferred_type == "timedelta",
            # Parse ISO 8601 duration format (e.g., "P1DT6H") to seconds
            # This extracts days, hours, minutes, seconds separately and converts to total seconds
            (
                # Days component (86400 seconds per day)
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
                +
                # Hours component (3600 seconds per hour)
                func.coalesce(
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
                +
                # Minutes component (60 seconds per minute)
                func.coalesce(
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
                +
                # Seconds component
                func.coalesce(
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
        (X.c.inferred_type == "float", X.c.value.cast(Float)),
        (X.c.inferred_type == "int", X.c.value.cast(Float)),
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


@router.get(
    "/logs/metric/{default_metric}",
    responses={
        200: {
            "description": "Successful Response",
            "content": {"application/json": {"example": 4.56}},
        },
        404: {
            "description": "Project Not Found",
            "content": {
                "application/json": {
                    "example": {"detail": "Project <project> not found."},
                },
            },
        },
    },
)
def get_logs_metric(
    request_fastapi: Request,
    default_metric: str = Path(...),
    project: str = Query(...),
    request: Optional[GetLogsMetricRequest] = Body(None),
    project_dao: ProjectDAO = Depends(),
    context_dao: ContextDAO = Depends(),
    field_type_dao: FieldTypeDAO = Depends(),
    session=Depends(get_db_session),
) -> Union[Dict[str, Any], float, int, bool, str, None]:
    """
    Returns the reduction metric for filtered values (base + derived) for one or more keys from a project.

    This endpoint supports three modes of operation:

    1. Single key, no grouping: Returns a single metric value
       Example: GET /logs/metric/mean?key=score
       Response: 4.56

    2. Multiple keys, no grouping: Returns a dict mapping keys to metric values
       Example: GET /logs/metric/mean?key=["score","length"]
       Response: {"score": 4.56, "length": 120}

    3. With grouping: Returns metrics grouped by one or more fields
       Example: GET /logs/metric/mean with body {"key": "score", "group_by": "model"}
       Response: {"gpt-4": 4.56, "gpt-3.5": 3.78}

       For nested grouping, provide a list of fields:
       Example: GET /logs/metric/mean with body {"key": "score", "group_by": ["model", "temperature"]}
       Response: {"gpt-4": {"0.7": 4.56, "0.9": 4.23}, "gpt-3.5": {"0.7": 3.78, "0.9": 3.45}}

    The group_by parameter can be a string for single-level grouping or a list of strings for
    nested grouping. Each group_by field can be prefixed with "params/" to indicate it's a parameter.
    """
    # Handle old usage if request body is not provided
    if request is None:
        key_param = request_fastapi.query_params.get("key")
        filter_expr = request_fastapi.query_params.get("filter_expr")
        from_ids = request_fastapi.query_params.get("from_ids")
        exclude_ids = request_fastapi.query_params.get("exclude_ids")
        context = request_fastapi.query_params.get("context")
        group_by = request_fastapi.query_params.get("group_by")

        if group_by and group_by.startswith("["):
            group_by = json.loads(group_by)
        if key_param is None:
            raise HTTPException(status_code=400, detail="Missing 'key' parameter.")

        # Parse JSON array syntax for 'key' or treat as single key
        if key_param.startswith("["):
            parsed_keys = json.loads(key_param)
            request = GetLogsMetricRequest(
                key=parsed_keys,
                filter_expr=filter_expr,
                from_ids=from_ids,
                exclude_ids=exclude_ids,
                context=context,
                group_by=group_by,
                metrics=None,
            )
        else:
            # Single key usage
            request = GetLogsMetricRequest(
                key=key_param,
                filter_expr=filter_expr,
                from_ids=from_ids,
                exclude_ids=exclude_ids,
                context=context,
                group_by=group_by,
                metrics=None,
            )

    # Get project and context
    try:
        user_id = request_fastapi.state.user_id
        project_obj = project_dao.get_by_user_and_name(name=project, user_id=user_id)
        project_id = project_obj.id
    except (IndexError, AttributeError):
        raise not_found(f"Project {project}")

    context_name = request.context or ""
    context_obj = context_dao.filter(name=context_name, project_id=project_obj.id)
    context_id = context_obj[0][0].id if context_obj else None
    field_types = field_type_dao.get_field_types(project_obj.id, context_id=context_id)

    if isinstance(request.from_ids, str) and isinstance(request.exclude_ids, str):
        raise HTTPException(
            status_code=400,
            detail="Cannot set both from_ids and exclude_ids at the top level.",
        )

    # Determine keys to compute
    if request.key is None:
        raise HTTPException(status_code=400, detail="No key(s) provided.")

    # Convert to list for processing
    if isinstance(request.key, str):
        all_keys = [request.key]
        single_key = True
    else:
        all_keys = request.key
        single_key = False

    # Check if group_by is provided
    if hasattr(request, "group_by") and request.group_by:
        # Compute grouped metrics for each key
        grouped_results = {}

        for k in all_keys:
            # Get metric for this key or use default
            per_key_metric = default_metric
            if request.metrics and k in request.metrics:
                per_key_metric = request.metrics[k]

            # Resolve key-specific filters
            (
                key_filter_expr,
                key_from_ids,
                key_exclude_ids,
            ) = _resolve_key_specific_filters(request, k)

            # Compute the grouped metric
            grouped_value = _compute_metric_for_key_grouped(
                key=k,
                metric=per_key_metric,
                project_obj=project_obj,
                context_id=context_id,
                field_types=field_types,
                group_by=request.group_by,
                key_filter_expr=key_filter_expr,
                key_from_ids=key_from_ids,
                key_exclude_ids=key_exclude_ids,
                session=session,
            )

            grouped_results[k] = grouped_value

        # If there's only one key, return just its grouped results
        if single_key:
            return grouped_results[all_keys[0]]

        return grouped_results
    else:
        # Original non-grouped behavior
        results = {}
        for k in all_keys:
            # Get metric for this key or use default
            per_key_metric = default_metric
            if request.metrics and k in request.metrics:
                per_key_metric = request.metrics[k]

            # Resolve key-specific filters
            (
                key_filter_expr,
                key_from_ids,
                key_exclude_ids,
            ) = _resolve_key_specific_filters(request, k)

            # Compute the metric
            value = compute_metric_for_key(
                key=k,
                metric=per_key_metric,
                project_obj=project_obj,
                context_id=context_id,
                field_types=field_types,
                key_filter_expr=key_filter_expr,
                key_from_ids=key_from_ids,
                key_exclude_ids=key_exclude_ids,
                session=session,
            )
            results[k] = value

        # Return single value or dictionary based on input type
        return results[all_keys[0]] if single_key else results


@router.get(
    "/logs/groups",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": [
                        {
                            "version": "v0",
                            "value": "First version of the system prompt",
                        },
                        {
                            "version": "v1",
                            "value": "Second version of the system prompt",
                        },
                    ],
                },
            },
        },
        404: {
            "description": "Project Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Project <project> not found.",
                    },
                },
            },
        },
    },
)
def get_log_groups(
    request_fastapi: Request,
    project: str = Query(
        description="Name of the project to get entries from.",
        example="eval-project",
    ),
    key: str = Query(
        description="Name of the log entry to get distinct values from.",
        example="system_prompt",
    ),
    filter_expr: Optional[str] = Query(
        None,
        description="Boolean string to filter entries before grouping.",
        example="len(output) > 200 and temperature == 0.5",
    ),
    from_ids: Optional[str] = Query(
        None,
        description="The log ids which are permitted to be included in the search. "
        "Each log id listed does not need to be returned, but no logs "
        "which are not included in this list can be returned. This "
        "argument *cannot* be set if `exclude_ids` is set.",
        example="0&1&2",
    ),
    exclude_ids: Optional[str] = Query(
        None,
        description="The log ids which cannot be returned from the search. "
        "None of the listed ids will be returned, even if the logs are "
        "valid as per the filtering expression etc. This argument *cannot* "
        "be set if `from_ids` is set.",
        example="0&1&2",
    ),
    project_dao: ProjectDAO = Depends(),
    field_type_dao: FieldTypeDAO = Depends(),
    context_dao: ContextDAO = Depends(),
    session=Depends(get_db_session),
) -> Dict[str, Any]:
    """
    Returns a dict with the different versions as keys and the values of the remaining
    items within a given project based on its key.
    The logs can be filtered using filter_expr, from_ids, and exclude_ids parameters
    before grouping.
    """
    # Get filtered logs using _get_logs_query
    # raw_rows is a list of:
    # - row_key
    # - row_value
    # - row_inferred_type
    # - row_version
    # - row_source_type
    # - row_created_at
    # - row_event_id
    raw_rows, _, _ = _get_logs_query(
        request_fastapi=request_fastapi,
        project=project,
        column_context=None,
        context=None,
        filter_expr=filter_expr,
        sorting=None,
        from_ids=from_ids,
        exclude_ids=exclude_ids,
        from_fields=key,  # Only get entries for the specified key
        exclude_fields=None,
        limit=None,
        offset=0,
        project_dao=project_dao,
        field_type_dao=field_type_dao,
        context_dao=context_dao,
        session=session,
    )

    groups = dict()
    for row in raw_rows:
        # Extract version and value from raw row
        version = row[3]  # version
        value = row[1]  # value

        if version is None:
            found_match = False
            for k, v in groups.items():
                if value in v:
                    version = k
                    found_match = True
                    break
            if not found_match:
                version = str(len(groups))
        if version not in groups:
            groups[str(version)] = set()
        groups[str(version)].add(value)
    assert all(
        len(v) == 1 for v in groups.values()
    ), "All sets should contain a single unique value"
    return {k: next(iter(v)) for k, v in groups.items()}


@router.post(
    "/logs/rename_field",
    responses={
        200: {
            "description": "Field renamed successfully",
            "content": {
                "application/json": {
                    "example": {
                        "info": "Field renamed successfully from 'old_name' to 'new_name'",
                    },
                },
            },
        },
        400: {
            "description": "Bad Request",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Invalid field name or field already exists",
                    },
                },
            },
        },
        404: {
            "description": "Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Project or field not found",
                    },
                },
            },
        },
    },
)
def rename_field(
    request_fastapi: Request,
    request: RenameFieldRequest,
    project_dao: ProjectDAO = Depends(),
    context_dao: ContextDAO = Depends(),
    field_type_dao: FieldTypeDAO = Depends(),
    log_dao: LogDAO = Depends(),
):
    """
    Renames a field across all logs in a project. This includes:
    - Updating the field type record
    - Renaming the field in all logs (regular and history)

    The operation is atomic - either all renames succeed or none do.
    """
    try:
        # Validate project and permissions
        user_id = request_fastapi.state.user_id
        project = project_dao.get_by_user_and_name(
            user_id=user_id,
            name=request.project,
        )

        if not project:
            raise HTTPException(
                status_code=404,
                detail=f"Project '{request.project}' not found",
            )
        project_id = project.id

        context_name = request.context if request.context else ""
        context = context_dao.filter(project_id=project_id, name=context_name)
        if not context:
            raise HTTPException(
                status_code=404,
                detail=f"Context '{context_name}' not found",
            )
        context_id = context[0][0].id

        # Validate new field name
        if not request.new_field_name:
            raise HTTPException(
                status_code=400,
                detail="Invalid field name: cannot be empty",
            )

        try:
            # Try to rename the field - this will raise ValueError if old field doesn't exist
            field_type_dao.rename_field(
                project_id=project_id,
                old_field_name=request.old_field_name,
                new_field_name=request.new_field_name,
                context_id=context_id,
            )
        except ValueError as e:
            if "does not exist" in str(e):
                raise HTTPException(
                    status_code=404,
                    detail="Field not found",
                )
            elif "already exists" in str(e):
                raise HTTPException(
                    status_code=400,
                    detail=str(e),
                )
            else:
                raise HTTPException(
                    status_code=400,
                    detail=str(e),
                )

        # Update all log records
        try:
            log_dao.rename_field_in_logs(
                project_id=project_id,
                old_field_name=request.old_field_name,
                new_field_name=request.new_field_name,
                context_id=context_id,
            )
        except ValueError as e:
            # Rollback the field type rename since log rename failed
            try:
                field_type_dao.rename_field(
                    project_id=project_id,
                    old_field_name=request.new_field_name,
                    new_field_name=request.old_field_name,
                    context_id=context_id,
                )
            except:
                pass
            raise HTTPException(
                status_code=400,
                detail=f"Failed to rename field in logs: {str(e)}",
            )

        return {
            "info": f"Field renamed successfully from '{request.old_field_name}' to '{request.new_field_name}'",
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error renaming field: {str(e)}",
        )


@router.get(
    "/logs/fields",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {
                        "field1": {
                            "data_type": "string",
                            "field_type": "entry",
                            "mutable": "true",
                            "created_at": "2025-02-14T10:00:00Z",
                            "artifacts": "",
                        },
                    },
                },
            },
        },
        404: {
            "description": "Project Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Project <project> not found.",
                    },
                },
            },
        },
    },
)
def get_fields(
    request_fastapi: Request,
    project: str = Query(
        description="Name of the project to get fields and their types for.",
        example="eval-project",
    ),
    context: Optional[str] = Query(
        "",
        description="Optional context name to filter field types",
        example="training",
    ),
    project_dao: ProjectDAO = Depends(),
    field_type_dao: FieldTypeDAO = Depends(),
    context_dao: ContextDAO = Depends(),
    session=Depends(get_db_session),
):
    """
    Returns a dictionary of field names and their types for the specified project.
    If a context is provided, returns only fields associated with that context.

    Each field entry contains:
    - data_type: The data type of the field (int, str, etc)
    - field_type: Whether it's an entry, param, or derived_entry
    - mutable: Whether the field can be modified
    - created_at: When the field was first created
    - artifacts: For derived entries, contains the equation
    """
    try:
        user_id = request_fastapi.state.user_id
        project_obj = project_dao.get_by_user_and_name(name=project, user_id=user_id)
        project_id = project_obj.id
    except (IndexError, AttributeError):
        raise not_found(f"Project {project}")

    # Get context_id if context is provided
    context_id = None
    if context:
        context_obj = context_dao.filter(project_id=project_obj.id, name=context)
    else:
        # use the default context
        context_obj = context_dao.filter(project_id=project_obj.id, name="")
        if not context_obj:
            return {}

    if not context_obj:
        raise HTTPException(
            status_code=404,
            detail=f"Context '{context}' not found",
        )
    context_id = context_obj[0][0].id

    # Get all field types with mutability info
    types = field_type_dao.get_field_types(
        project_obj.id,
        context_id=context_id,
        return_mutable=True,
    )
    # For derived entries, get their equations
    derived_equations = {}
    derived_fields = (
        session.query(DerivedLog.key, DerivedLog.equation)
        .join(LogEvent, LogEvent.id == DerivedLog.log_event_id)
        .filter(LogEvent.project_id == project_obj.id)
        .distinct()
        .all()
    )
    for key, equation in derived_fields:
        derived_equations[key] = equation

    # Build response
    return {
        key: {
            "data_type": info["field_type"],
            "field_type": info["field_category"],
            "mutable": info["mutable"],
            "created_at": info["created_at"],
            "artifacts": derived_equations.get(key, ""),
        }
        for key, info in types.items()
    }


#####################
# GroupBy Utils     #
#####################

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
        value_col = Log.version
        subquery = (
            session.query(
                value_col.label("value"),
                Log.log_event_id,
                func.row_number()
                .over(
                    partition_by=value_col,
                    order_by=desc(Log.log_event_id),
                )
                .label("rn"),
            )
            .filter(Log.log_event_id.in_(log_event_ids))
            .filter(Log.key == group_key)
            .subquery()
        )
    else:
        # For non-parameters, union base logs and derived logs
        base_query = (
            session.query(
                Log.value.label("value"),
                Log.log_event_id.label("log_event_id"),
            )
            .filter(Log.log_event_id.in_(log_event_ids))
            .filter(Log.key == group_key)
        )

        derived_query = (
            session.query(
                DerivedLog.value.label("value"),
                DerivedLog.log_event_id.label("log_event_id"),
            )
            .filter(DerivedLog.log_event_id.in_(log_event_ids))
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
            session.query(Log.log_event_id)
            .filter(Log.log_event_id.in_(log_event_ids))
            .filter(Log.key == group_key)
            .filter(Log.version == group_value)
        )
    elif group_key == "derived_entries":
        # For derived entries, only search derived logs
        query = (
            session.query(DerivedLog.log_event_id)
            .filter(DerivedLog.log_event_id.in_(log_event_ids))
            .filter(DerivedLog.key == group_key)
            .filter(cast(DerivedLog.value, JSONB) == cast(group_value, JSONB))
        )
    else:
        # For non-parameters, search both base and derived logs
        base_query = (
            session.query(Log.log_event_id)
            .filter(Log.log_event_id.in_(log_event_ids))
            .filter(Log.key == group_key)
            .filter(cast(Log.value, JSONB) == cast(group_value, JSONB))
        )

        derived_query = (
            session.query(DerivedLog.log_event_id)
            .filter(DerivedLog.log_event_id.in_(log_event_ids))
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
        .filter(Log.log_event_id.in_(select(log_event_ids)))
        .filter(Log.version.isnot(None))
    )

    params = {}
    for log in query.all():
        if log.key not in params:
            params[log.key] = {}
        params[log.key][log.version] = log.value

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
    return_versions: bool = False,
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

    # Handle ID filtering differently based on return_versions
    if return_versions:
        if from_ids:
            try:
                # Validate from_ids format for versioned logs
                from_ids = json.loads(from_ids)
                if not isinstance(from_ids, list):
                    raise ValueError(
                        "from_ids must be a list when return_versions is True",
                    )
                for item in from_ids:
                    if (
                        not isinstance(item, dict)
                        or "id" not in item
                        or "version" not in item
                    ):
                        raise ValueError(
                            "Each item in from_ids must have 'id' and 'version' keys",
                        )
                allowed_pairs = [(item["id"], item["version"]) for item in from_ids]
                # Apply filtering at the Log/LogHistory level since we need version info
                filtered_logs_q = filtered_logs_q.filter(
                    tuple_(
                        LogHistory.log_event_id,
                        LogHistory.context_version,
                    ).in_(allowed_pairs),
                )
            except ValueError as e:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid from_ids format for versioned logs: {str(e)}",
                )
        if exclude_ids:
            try:
                # Validate exclude_ids format for versioned logs
                exclude_ids = json.loads(exclude_ids)
                if not isinstance(exclude_ids, list):
                    raise ValueError(
                        "exclude_ids must be a list when return_versions is True",
                    )
                for item in exclude_ids:
                    if (
                        not isinstance(item, dict)
                        or "id" not in item
                        or "version" not in item
                    ):
                        raise ValueError(
                            "Each item in exclude_ids must have 'id' and 'version' keys",
                        )
                excluded_pairs = [(item["id"], item["version"]) for item in exclude_ids]
                # Apply filtering at the Log/LogHistory level since we need version info
                filtered_logs_q = filtered_logs_q.filter(
                    ~tuple_(
                        LogHistory.log_event_id,
                        LogHistory.context_version,
                    ).in_(excluded_pairs),
                )
            except ValueError as e:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid exclude_ids format for versioned logs: {str(e)}",
                )
    else:
        # For non-versioned queries, use simple log_event_id filtering
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
    return_versions: bool = False,
) -> Union[Tuple[List[Tuple[Union[Log, DerivedLog], datetime, int]], int], str]:
    """
    Given a known list of event_ids, retrieve the union of Log + DerivedLog rows
    that match column_context, from_fields/exclude_fields, etc. Then apply sorting
    + pagination to the distinct event_ids, and return (rows, count).
    If latest_timestamp=True, return only the max updated_at across those logs.
    """
    # Check if event_ids is empty
    if isinstance(event_ids, list):
        if not event_ids:
            return ([], 0) if not latest_timestamp else None
    else:
        # For subquery, check if it returns any rows
        count_check = session.query(event_ids.c.id).limit(1).all()
        if not count_check:
            return ([], 0) if not latest_timestamp else None

    # Create a CTE from either the list or use the subquery directly
    if isinstance(event_ids, list):
        event_ids_cte = (
            session.query(LogEvent.id.label("id"))
            .filter(LogEvent.id.in_(event_ids))
            .cte("event_ids_cte")
        )
    else:
        # If event_ids is already a subquery, use it directly
        event_ids_cte = event_ids
    # 1) Build union subquery from base logs + derived logs, for these event IDs
    unified_logs_subq = _build_unified_logs_subquery(
        session=session,
        relevant_log_events=event_ids_cte,
        return_versions=return_versions,
    )

    # 2) column_context logic + exclude_params/entries
    exclude_params = False
    exclude_entries = False
    context_len = 0
    if column_context:
        parts = column_context.split("/")
        exclude_params = "entries" in parts
        exclude_entries = "params" in parts
        # Clean out those tokens
        cleaned_parts = [x for x in parts if x not in ("entries", "params")]
        real_prefix = "/".join(cleaned_parts)
        if real_prefix and not real_prefix.endswith("/"):
            real_prefix += "/"

        filtered_logs_q = session.query(unified_logs_subq).filter(True)
        if real_prefix:
            filtered_logs_q = filtered_logs_q.filter(
                unified_logs_subq.c.key.startswith(real_prefix),
            )
            context_len = len(real_prefix)
    else:
        filtered_logs_q = session.query(unified_logs_subq)

    # TODO(yusha): handle filtering out corresponding rows from LogHistory as well
    if exclude_params:
        filtered_logs_q = filtered_logs_q.filter(
            unified_logs_subq.c.param_version.is_(None),
        )
    elif exclude_entries:
        filtered_logs_q = filtered_logs_q.filter(
            unified_logs_subq.c.param_version.isnot(None),
        )

    # 3) from_fields / exclude_fields
    if from_fields and exclude_fields:
        raise HTTPException(
            status_code=400,
            detail="Cannot set both from_fields and exclude_fields.",
        )
    if from_fields:
        allowed = from_fields.split("&")
        filtered_logs_q = filtered_logs_q.filter(unified_logs_subq.c.key.in_(allowed))
    elif exclude_fields:
        excluded = exclude_fields.split("&")
        filtered_logs_q = filtered_logs_q.filter(
            unified_logs_subq.c.key.notin_(excluded),
        )
    if parent_fields:
        # We want to exclude any logs with the parent_fields as these
        # are the ones we're grouping by.
        not_allowed = parent_fields.split("&")
        filtered_logs_q = filtered_logs_q.filter(
            unified_logs_subq.c.key.notin_(not_allowed),
        )

    filtered_logs_subq = filtered_logs_q.subquery("filtered_logs_subq")

    # 4) Distinct log_event_ids => sort => limit => offset
    distinct_ids_subq = (
        session.query(filtered_logs_subq.c.log_event_id.label("log_event_id"))
        .distinct()
        .subquery("distinct_ids_subq")
    )

    context_name = "" if not context else context
    context_id = context_dao.filter(name=context_name, project_id=project_id)[0][0].id
    field_types = field_type_dao.get_field_types(project_id, context_id=context_id)
    sorted_query = session.query(distinct_ids_subq.c.log_event_id)
    sort_criteria = []

    # 4a) Sorting
    if sorting:
        sort_dict = json.loads(sorting)
        for sort_key, mode in sort_dict.items():
            if mode not in ("ascending", "descending"):
                raise HTTPException(
                    status_code=400,
                    detail=f"Sort mode must be 'ascending' or 'descending'; got {mode}",
                )
            key_subq = (
                session.query(
                    filtered_logs_subq.c.log_event_id.label("log_event_id"),
                    filtered_logs_subq.c.value.label("raw_value"),
                )
                .filter(filtered_logs_subq.c.key == sort_key)
                .subquery(f"sort_{sort_key}_subq")
            )
            sorted_query = sorted_query.outerjoin(
                key_subq,
                key_subq.c.log_event_id == distinct_ids_subq.c.log_event_id,
            )
            direction = asc if mode == "ascending" else desc
            if sort_key in field_types:
                pytype = field_types[sort_key]
                cast_type = STR_TO_SQL_TYPES.get(pytype, None)
                if cast_type is not None:
                    if pytype == "timestamp":
                        # For timestamps, we need to first cast to text and then to timestamp
                        sort_expr = case(
                            (key_subq.c.raw_value.is_(None), None),
                            (key_subq.c.raw_value == text("'null'::jsonb"), None),
                            # Cast to text first, then to timestamp
                            else_=cast(cast(key_subq.c.raw_value, String), cast_type),
                        )
                    elif pytype in ("dict", "list"):
                        # For JSONB types, no need for additional casting
                        sort_expr = key_subq.c.raw_value
                    else:
                        # For other data types (bool, int, float, str)
                        sort_expr = case(
                            (key_subq.c.raw_value.is_(None), None),
                            (key_subq.c.raw_value == text("'null'::jsonb"), None),
                            else_=cast(key_subq.c.raw_value, cast_type),
                        )
                else:
                    sort_expr = key_subq.c.raw_value
            else:
                sort_expr = key_subq.c.raw_value

            sort_criteria.append(direction(sort_expr).nulls_last())

    # If not sorted, fallback to ID desc
    if not sort_criteria:
        sort_criteria.append(distinct_ids_subq.c.log_event_id.desc())

    sorted_query = sorted_query.add_columns(
        func.row_number().over(order_by=sort_criteria).label("row_num"),
    ).order_by("row_num")

    # 4b) Apply pagination
    total_count = sorted_query.count()
    if limit:
        sorted_query = sorted_query.limit(limit)
    if offset:
        sorted_query = sorted_query.offset(offset)

    if latest_timestamp:
        # If we only want the max updated_at
        max_updated_at = (
            session.query(func.max(filtered_logs_subq.c.updated_at))
            .filter(filtered_logs_subq.c.log_event_id.in_(event_ids))
            .scalar()
        )
        return max_updated_at.isoformat() if max_updated_at else None

    paginated_ids_subq = sorted_query.subquery("paginated_ids_subq")

    # 5) Finally join back to get the actual rows
    raw_rows = _get_final_logs(session, filtered_logs_subq, paginated_ids_subq)

    # 6) Return the raw rows so that the top-level get_logs can do the final formatting.
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
    cast_expr = case(
        # Handle NULL values first
        (aggCol.is_(None), literal(None, type_=Float)),
        (
            inferred_type == "list",
            func.jsonb_array_length(cast(aggCol, JSONB)).cast(Float),
        ),
        (
            inferred_type == "dict",
            select(func.count())
            .select_from(func.jsonb_object_keys(cast(aggCol, JSONB)))
            .scalar_subquery()
            .cast(Float),
        ),
        (
            inferred_type == "bool",
            aggCol.cast(BOOLEAN).cast(INTEGER).cast(Float),
        ),
        (
            inferred_type == "str",
            func.length(cast(aggCol, JSONB)[0].astext).cast(Float),
        ),
        (
            inferred_type == "timestamp",
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
                func.nullif(cast(aggCol.op("->>")(0), String), "null").cast(Float),
                None,
            ).cast(Float),
        ),
        (
            inferred_type == "float",
            func.coalesce(
                func.nullif(cast(aggCol.op("->>")(0), String), "null").cast(Float),
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


def _handle_group_depth_level(
    session,
    log_event_ids: Union[List[int], Subquery],
    field_types,
    group_by,
    group_sorting,
    group_limit,
    group_offset,
    level,
    return_versions,
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
        return_versions=return_versions,
    )

    # Group by value and filter on the raw key
    field_to_compare = (
        unified.c.param_version if prefix == "params" else unified.c.value
    )
    base_q = (
        session.query(
            field_to_compare.label("group_value"),
            func.max(unified.c.log_event_id).label("log_event_id"),
            func.count(func.distinct(unified.c.log_event_id)).label("log_count"),
        )
        .filter(
            unified.c.log_event_id.in_(select(log_event_ids)),
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
                        base_alias.c.value.label("group_key_value")
                        if prefix != "params"
                        else base_alias.c.param_version.label("group_key_value"),
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
        log_count = row.log_count
        group_list.append({"key": str(group_val), "value": log_count})

    # Find missing IDs (logs that don't have this key)
    present_value_q = _build_unified_logs_subquery(
        session=session,
        relevant_log_events=event_ids_cte,
        return_versions=return_versions,
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
    return_versions: bool = False,
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
            return_versions=return_versions,
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
            return_versions=return_versions,
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
        return_versions=return_versions,
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
                    base_alias.c.value.label("group_key_value")
                    if prefix != "params"
                    else base_alias.c.param_version.label("group_key_value"),
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
            return_versions=return_versions,
            parent_group_key="&".join([parent_group_key, raw_key])
            if parent_group_key
            else raw_key,
        )

        # Add to group list instead of directly to result_dict
        group_list.append({"key": str(group_val), "value": substructure})
    # find missing IDs (logs that don't have this key)
    present_value_q = _build_unified_logs_subquery(
        session=session,
        relevant_log_events=event_ids_cte,
        return_versions=return_versions,
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
            return_versions=return_versions,
            parent_group_key="&".join([parent_group_key, raw_key])
            if parent_group_key
            else raw_key,
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


#########################
# GET Logs Utils        #
#########################
# TODO(yusha): refactor get_logs_query to make it modular
def _build_unified_logs_subquery(
    session,
    event_ids: Optional[Subquery] = None,
    relevant_log_events: Optional[Subquery] = None,
    key: str = None,
    return_versions: bool = False,
) -> Subquery:
    """
    Build a unified subquery that combines base logs and derived logs based on return_versions parameter.

    Args:
        session: The database session
        event_ids: Optional list of event IDs to filter by directly
        relevant_log_events: Optional subquery containing relevant log event IDs to join with
        return_versions: Whether to include version history in the query

    Returns:
        A unified subquery combining base and derived logs
    """
    if event_ids is None and relevant_log_events is None:
        raise ValueError("Either event_ids or relevant_log_events must be provided")

    def _apply_event_filter(query, table):
        if event_ids is not None:
            return query.filter(LogEvent.id.in_(event_ids))
        query = query.join(relevant_log_events, relevant_log_events.c.id == LogEvent.id)
        if key:
            return query.filter(table.key == key)
        return query

    if return_versions:
        # get latest version + all history logs
        base_logs_q_current = session.query(
            Log.id.label("id"),
            Log.log_event_id.label("log_event_id"),
            Log.key.label("key"),
            Log.value.label("value"),
            Log.inferred_type.label("inferred_type"),
            Log.version.label("param_version"),
            cast(None, Integer).label("context_version"),
            Log.updated_at.label("updated_at"),
            LogEvent.created_at.label("created_at"),
            literal("current").label("source_type"),
        ).join(LogEvent, LogEvent.id == Log.log_event_id)
        base_logs_q_current = _apply_event_filter(base_logs_q_current, Log)

        base_logs_q_history = session.query(
            LogHistory.id.label("id"),
            LogHistory.log_event_id.label("log_event_id"),
            LogHistory.key.label("key"),
            LogHistory.value.label("value"),
            LogHistory.inferred_type.label("inferred_type"),
            cast(None, Integer).label("param_version"),
            LogHistory.version.label("context_version"),
            LogHistory.archived_at.label("updated_at"),
            LogEvent.created_at.label("created_at"),
            literal("history").label("source_type"),
        ).join(LogEvent, LogEvent.id == LogHistory.log_event_id)
        base_logs_q_history = _apply_event_filter(base_logs_q_history, LogHistory)

        base_logs_q = base_logs_q_current.union_all(base_logs_q_history)
    else:
        # get only the latest version of the logs
        base_logs_q = session.query(
            Log.id.label("id"),
            Log.log_event_id.label("log_event_id"),
            Log.key.label("key"),
            Log.value.label("value"),
            Log.inferred_type.label("inferred_type"),
            Log.version.label("param_version"),
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
    return select(
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


@admin_router.post(
    "/update_active_derived_logs",
    responses={
        200: {
            "description": "Active derived logs updated successfully",
            "content": {
                "application/json": {
                    "example": {
                        "info": "Processed 5 templates and created 42 derived logs",
                    },
                },
            },
        },
        500: {
            "description": "Internal Server Error",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Error processing active derived log templates",
                    },
                },
            },
        },
    },
)
def update_active_derived_logs(
    session=Depends(get_db_session),
    field_type_dao: FieldTypeDAO = Depends(),
    _=Depends(auth_admin_key),
):
    """
    Admin endpoint to process active derived logs and create new derived logs
    for new log events that match the filter criteria.
    This endpoint  is designed to be calledby internal processes (e.g., Cloud Scheduler) or administrators.
    """

    try:
        # Get all active templates
        active_templates = (
            session.query(ActiveDerivedLog)
            .filter(ActiveDerivedLog.is_active == True)
            .all()
        )

        if not active_templates:
            return {"info": "No active templates found"}

        total_derived_logs_created = 0

        # Process each template
        for template in active_templates:
            # Get field types for the project
            field_types = field_type_dao.get_field_types(
                template.project_id,
                context_id=template.context_id,
            )

            # Find log events that don't already have this derived log
            # First, get all log events for this project
            all_log_events = (
                session.query(LogEvent.id)
                .filter(LogEvent.project_id == template.project_id)
                .subquery(name="all_log_events")
            )

            # Then, get log events that already have this derived log
            existing_derived_logs = (
                session.query(DerivedLog.log_event_id)
                .filter(
                    DerivedLog.key == template.key,
                    DerivedLog.log_event_id.in_(select(all_log_events.c.id)),
                )
                .subquery(name="existing_derived_logs")
            )

            # Find log events that don't have this derived log yet
            new_log_events = (
                session.query(LogEvent.id)
                .filter(
                    LogEvent.id.in_(select(all_log_events.c.id)),
                    ~LogEvent.id.in_(select(existing_derived_logs.c.log_event_id)),
                )
                .subquery(name="new_log_events")
            )

            # If there are no new log events, skip this template
            if session.query(new_log_events).count() == 0:
                continue

            # Prepare the filter expression
            try:
                # Get all log events that match the filter expression
                log_event_ids_subq = (
                    session.query(LogEvent.id)
                    .filter(
                        LogEvent.project_id == template.project_id,
                        LogEvent.id.in_(select(new_log_events.c.id)),
                    )
                    .subquery(name="log_event_ids_subq")
                )

                # Apply the filter expression to find matching log events
                filter_dict = None
                resolved_ids = {}
                matching_log_event_ids = log_event_ids_subq
                # If we have a filter expression in the template
                if template.filter_expression:
                    # For each alias in the filter expression
                    for alias, filter_config in template.filter_expression.items():
                        if (
                            isinstance(filter_config, dict)
                            and "filter_expr" in filter_config
                        ):
                            try:
                                # Convert the filter expression to a filter dict
                                filter_dict = str_filter_exp_to_dict(
                                    filter_config["filter_expr"],
                                    field_names=list(field_types.keys()),
                                )

                                # Apply the filter to find matching log events
                                condition = build_sql_query(
                                    filter_dict,
                                    LogEvent,
                                    session,
                                    log_event_ids=log_event_ids_subq,
                                )
                            except Exception as e:
                                condition = None  # If the filter expression is empty (eg: filter_expr: '')

                            # Get the log event IDs that match the filter
                            if isinstance(condition, Subquery):
                                matching_log_events = (
                                    session.query(LogEvent.id)
                                    .filter(
                                        LogEvent.id.in_(
                                            select(log_event_ids_subq.c.id),
                                        ),
                                        exists(
                                            select(1)
                                            .select_from(condition)
                                            .where(
                                                and_(
                                                    condition.c.log_event_id
                                                    == LogEvent.id,
                                                    condition.c.value.is_(True),
                                                ),
                                            ),
                                        ),
                                    )
                                    .all()
                                )
                            else:
                                matching_log_events = session.query(
                                    log_event_ids_subq.c.id,
                                ).all()

                            # Extract the log event IDs
                            matching_log_event_ids = [
                                row[0] for row in matching_log_events
                            ]

                            # If no matching log events, skip this template
                            if not matching_log_event_ids:
                                continue

                            resolved_ids[alias] = matching_log_event_ids
                    # Compute the derived values for each matching log event
                    filter_expr, alias_to_key_map = _substitute_placeholders(
                        template.equation,
                        resolved_ids,
                    )
                    filter_dict = str_filter_exp_to_dict(
                        filter_expr,
                        field_names=list(field_types.keys()),
                    )
                    computed_values = _compute_expression(
                        filter_dict,
                        LogEvent,
                        session,
                        log_event_ids=matching_log_event_ids,
                    )

                    # Create derived logs for each matching log event
                    new_derived_logs = []

                    for log_event_id, (_, value) in zip(
                        matching_log_event_ids,
                        computed_values,
                    ):
                        val = json.loads(json.dumps(value, cls=CustomEncoder))
                        inferred_type = LogDAO.infer_type("", val)

                        new_derived_logs.append(
                            DerivedLog(
                                log_event_id=log_event_id,
                                key=template.key,
                                equation=template.equation,
                                referenced_logs=template.referenced_logs,
                                value=val,
                                inferred_type=inferred_type,
                                created_at=datetime.now(timezone.utc),
                                updated_at=datetime.now(timezone.utc),
                            ),
                        )

                    # Bulk insert the new derived logs
                    if new_derived_logs:
                        session.bulk_save_objects(new_derived_logs)
                        total_derived_logs_created += len(new_derived_logs)

            except Exception as e:
                raise HTTPException(
                    status_code=500,
                    detail=f"Error processing template {template.id}: {str(e)}",
                )
        # Commit all changes
        session.commit()

        return {
            "info": f"Created {total_derived_logs_created} new derived logs",
        }

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error processing active derived log templates: {str(e)}",
        )


@admin_router.post(
    "/process_traffic_logs",
    responses={
        200: {
            "description": "PubSub messages pulled and processed successfully",
            "content": {
                "application/json": {
                    "example": {
                        "message": "Pulled and processed 10 messages",
                        "status": "success",
                    },
                },
            },
        },
        500: {
            "description": "Internal Server Error",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Error processing traffic logs",
                    },
                },
            },
        },
    },
)
async def process_traffic_logs(
    max_messages: int = Query(100, description="Maximum number of messages to pull"),
    session=Depends(get_db_session),
    project_dao: ProjectDAO = Depends(),
    log_event_dao: LogEventDAO = Depends(),
    log_dao: LogDAO = Depends(),
    field_type_dao: FieldTypeDAO = Depends(),
    context_dao: ContextDAO = Depends(),
    _=Depends(auth_admin_key),
):
    """
    Admin endpoint to manually pull and process traffic log messages from PubSub.
    This endpoint is designed to be called by internal processes (e.g., Cloud Scheduler) or administrators.
    """
    try:
        from google.cloud import pubsub_v1

        from orchestra.settings import settings

        # Configure the subscriber client
        subscriber = pubsub_v1.SubscriberClient()
        subscription_path = subscriber.subscription_path(
            settings.traffic_log_pubsub_project_id,
            settings.traffic_log_pubsub_subscription,
        )

        # The maximum number of messages to return
        pull_limit = max_messages

        # Pull messages from PubSub
        response = subscriber.pull(
            request={"subscription": subscription_path, "max_messages": pull_limit},
        )

        processed_count = 0
        ack_ids = []

        # Process the received messages
        for received_message in response.received_messages:
            message = received_message.message
            ack_ids.append(received_message.ack_id)

            # Decode the message data
            message_data = message.data.decode("utf-8")
            entry = json.loads(message_data)

            # Extract fields from the entry
            project_id = entry.pop("project_id")
            context_id = entry.pop("context_id")
            project_name = entry.pop("project_name")

            # Create log entry
            event_ids = create_logs_internal(
                project_id=project_id,
                context_id=context_id,
                request=CreateLogConfig(
                    entries=entry,
                    project=project_name,
                    context=None,
                ),
                project_dao=project_dao,
                field_type_dao=field_type_dao,
                log_event_dao=log_event_dao,
                log_dao=log_dao,
                context_dao=context_dao,
            )

            processed_count += 1

        # Acknowledge the processed messages
        if ack_ids:
            subscriber.acknowledge(
                request={"subscription": subscription_path, "ack_ids": ack_ids},
            )

        # Close the subscriber client
        subscriber.close()

        return {
            "message": f"Pulled and processed {processed_count} messages",
            "status": "success",
        }

    except Exception as e:
        import traceback

        traceback.print_exc()
        return JSONResponse(
            status_code=500,
            content={"detail": f"Error processing traffic logs: {str(e)}"},
        )
