"""

Includes endpoints related to Log API.
"""

import json
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple, Union

import numpy as np
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
from sqlalchemy import and_, exists, or_, select, update
from sqlalchemy.sql.selectable import Subquery

from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dao.derived_log_dao import DerivedLogDAO
from orchestra.db.dao.field_type_dao import FieldTypeDAO
from orchestra.db.dao.log_dao import ImmutableFieldError, LogDAO, OverwriteError
from orchestra.db.dao.log_event_dao import LogEventDAO
from orchestra.db.dao.organization_dao import OrganizationDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.project_dao import ProjectDAO
from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import (
    ActiveDerivedLog,
    Context,
    DerivedLog,
    Embedding,
    Log,
    LogEvent,
    LogEventContext,
    LogEventDerivedLog,
    LogEventLog,
)
from orchestra.web.api.dependencies import auth_admin_key
from orchestra.web.api.log.schema import (
    CreateDerivedEntriesConfig,
    CreateFieldsRequest,
    CreateLogConfig,
    DeleteFieldsRequest,
    DeleteLogEntryRequest,
    GetLogsMetricRequest,
    JoinLogsRequest,
    QueryLogsPostBody,
    RenameFieldRequest,
    UpdateDerivedEntriesConfig,
    UpdateLogRequest,
)
from orchestra.web.api.utils.helpers import CustomEncoder
from orchestra.web.api.utils.http_responses import not_found

from .python2SQL import (
    DEFAULT_EMBEDDING_MODEL,
    _compute_expression,
    _extract_placeholders,
    _substitute_placeholders,
    build_sql_query,
    str_filter_exp_to_dict,
)
from .utils import (
    _build_grouped_data,
    _compute_metric_for_key_grouped,
    _fetch_logs_for_event_ids,
    _flatten_fields,
    _format_flat_logs,
    _get_all_filtered_log_event_ids,
    _get_distinct_group_values,
    _get_log_event_ids_for_group_value,
    _get_logs_query,
    _get_params_for_log_events,
    _join_logs,
    _resolve_key_specific_filters,
    apply_group_threshold,
    compute_metric_bulk,
    compute_metric_for_key,
    create_logs_internal,
)

router = APIRouter()

# Admin router for protected endpoints
admin_router = APIRouter()


###########################
# endpoints
###########################


@router.post(
    "/logs",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {
                        "info": "Logs created successfully!",
                        "log_event_ids": [101, 102, 103],
                        "row_ids": {"names": ["row_id"], "ids": [[0], [1], [2]]},
                        "auto_counting": {
                            "row_id": [0, 1, 2],
                            "exchange_id": [0, 1, 2],
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
    session=Depends(get_db_session),
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
    can also specify if a field is mutable via a 'mutable' boolean flag
    and if a field is unique via a 'unique' boolean flag:

    ```json
    {
        "field_name": {
            "type": "str",
            "mutable": false,  # Makes the field immutable
            "unique": true     # Makes the field unique
        }
    }
    ```

    By default, all fields are immmutable unless specified otherwise.
    Once a field is marked as mutable, only then can it be modified through
    the update endpoint.

    **Response includes:**
    - `log_event_ids`: List of created log event IDs
    - `row_ids`: Object with `names` (unique key column names) and `ids` (nested list of values)
    - `auto_counting`: Dictionary mapping auto-counting column names to their generated/provided values.
      Empty dict `{}` when no auto-counting is configured.

    This method returns the ids of the new stored logs along with any auto-counting values.
    """
    # Instantiate DAOs with shared session (types may be strings or JSON schemas)
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    field_type_dao = FieldTypeDAO(session)
    log_event_dao = LogEventDAO(session)
    log_dao = LogDAO(session, context_dao)

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

    # Load the Context object once
    context_obj = session.get(Context, context_id)

    try:
        # Call the internal implementation with validated project and context
        result = create_logs_internal(
            request=request,
            project_id=project_id,
            context_id=context_id,
            context_obj=context_obj,
            project_dao=project_dao,
            field_type_dao=field_type_dao,
            log_event_dao=log_event_dao,
            log_dao=log_dao,
            context_dao=context_dao,
        )

        # Final sanity: if nothing succeeded and there are failures, surface 400
        if not result.get("log_event_ids") and result.get("failed"):
            first_error = result["failed"][0].get("error", "Log creation failed")
            raise HTTPException(status_code=400, detail=first_error)

        return {
            "info": "Logs created successfully!",
            "log_event_ids": result["log_event_ids"],
            "row_ids": result["row_ids"],
            "auto_counting": result["auto_counting"],
            "failed": result.get("failed", []),
        }
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


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
def create_from_logs(
    request_fastapi: Request,
    body: CreateDerivedEntriesConfig,
    session=Depends(get_db_session),
):
    """
    Creates one or more entries based on `body.equation` and `body.referenced_logs`.

    When body.derived=True (default):
      Eagerly computes each derived value and stores it in DerivedLog.value.

    When body.derived=False:
      Computes values and stores them directly in the base logs as regular entries.

    The context parameter can be:
    - A string: Uses the string as the context name with default values (description=None, is_versioned=False)
    - An object: Uses the object's name, description, and is_versioned properties
    """
    # Instantiate DAOs with shared session
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    field_type_dao = FieldTypeDAO(session)
    log_dao = LogDAO(session, context_dao)

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

    # Resolve IDs for both derived and non-derived paths
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

    # Branch based on whether we're creating derived logs or static entries
    if not body.derived:
        # Create static entries in base logs
        try:
            # 1) Substitute placeholders and prepare for computation
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

            # 2) Get the filtered log events
            log_event_ids_subq = (
                session.query(LogEvent.id)
                .filter(project_obj.id == LogEvent.project_id)
                .subquery(name="log_event_ids_subq")
            )

            # 3) Compute values
            computed_values = _compute_expression(
                filter_dict,
                LogEvent,
                session,
                log_event_ids=log_event_ids_subq,
            )

            # 4) Prepare updates for bulk_update
            updates = []
            non_null_value = None
            updated_log_ids = []

            for log_event_id, value in computed_values:
                updated_log_ids.append(log_event_id)
                val = json.loads(json.dumps(value, cls=CustomEncoder))
                non_null_val = val if val is not None else non_null_value

                updates.append(
                    {
                        "log_event_id": log_event_id,
                        "key": body.key,
                        "value": val,
                        "context_id": context_id,
                        "overwrite": True,
                    },
                )

            # 5) Perform bulk update
            if updates:
                log_dao.bulk_update(updates, overwrite=True, field_types=field_types)

                # 6) Create or update field type record
                # Use infer_type=True to infer type from value (no explicit_types here)
                field_type_dao.create_field_type_if_absent(
                    project_id=project_obj.id,
                    field_name=body.key,
                    value=non_null_val,
                    field_category="entry",
                    context_id=context_id,
                    infer_type=True,  # Infer type from value for static entries
                )

                session.commit()

                return {
                    "info": f"Created {len(updates)} static entries with key='{body.key}'.",
                }
            else:
                return {"info": "No entries created."}

        except Exception as e:
            session.rollback()
            raise HTTPException(
                status_code=500,
                detail=f"Failed to create static entries with key='{body.key}'. Error: {e}",
            )
    else:
        # Original derived log creation logic
        try:
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

            # Flatten all referenced log_event_ids across aliases
            filtered_log_ids = list(
                {int(i) for ids in resolved_ids_dict.values() for i in ids},
            )

            # Get the filtered log events scoped to provided IDs
            log_event_ids_subq = (
                session.query(LogEvent.id)
                .join(LogEventContext, LogEvent.id == LogEventContext.log_event_id)
                .filter(LogEvent.project_id == project_obj.id)
                .filter(
                    LogEventContext.context_id == context_id,
                    LogEvent.id.in_(filtered_log_ids),
                )
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
            derived_log_associations = []  # Track (log_event_id, derived_log_index)
            placeholders = _extract_placeholders(body.equation)
            referenced_logs = {
                ph.split(":")[1]: v
                for ph in placeholders
                for k, v in body.referenced_logs.items()
                if k in ph
            }
            # Create index mappings for each alias to track position of log_event_ids
            # This helps us find corresponding log_event_ids across different aliases
            alias_to_id_list = {}
            alias_to_index_map = {}

            for alias, id_list in resolved_ids.items():
                alias_to_id_list[alias] = id_list
                # Create a mapping from log_event_id to its index position for this alias
                alias_to_index_map[alias] = {
                    log_id: idx for idx, log_id in enumerate(id_list)
                }

            # Process each computed value
            non_null_val = None
            for computed_log_id, value in computed_values:
                # Find which alias and index this computed_log_id belongs to
                source_alias = None
                source_index = None

                for alias, index_map in alias_to_index_map.items():
                    if computed_log_id in index_map:
                        source_alias = alias
                        source_index = index_map[computed_log_id]
                        break

                # If we found the source, create derived logs for all corresponding log_event_ids
                if source_index is not None:
                    # For each alias, get the log_event_id at the same index position
                    for alias, id_list in alias_to_id_list.items():
                        if source_index < len(id_list):
                            log_event_id = id_list[source_index]

                            val = json.loads(json.dumps(value, cls=CustomEncoder))
                            non_null_val = val if val is not None else non_null_val
                            inferred_type = LogDAO.infer_type("", non_null_val)

                            # Create a derived entry for this log ID
                            if isinstance(value, np.ndarray):
                                # Determine if this is an image embedding or text embedding
                                # by checking if the equation contains embed_image()
                                is_image_embedding = "embed_image(" in body.equation

                                # Use appropriate model name based on embedding type
                                if is_image_embedding:
                                    from orchestra.web.api.log.python2SQL.helpers import (
                                        DEFAULT_IMAGE_EMBEDDING_MODEL,
                                    )

                                    model_name = DEFAULT_IMAGE_EMBEDDING_MODEL
                                else:
                                    model_name = DEFAULT_EMBEDDING_MODEL

                                # add the embedding to the vector index table
                                embeddings = Embedding(
                                    ref_id=log_event_id,
                                    key=body.key,
                                    model=model_name,
                                    vector=value,
                                )
                                session.add(embeddings)

                            # Create DerivedLog without log_event_id
                            new_derived_logs.append(
                                DerivedLog(
                                    key=body.key,
                                    equation=body.equation,
                                    referenced_logs=referenced_logs,
                                    value=val,
                                    inferred_type=inferred_type,
                                    created_at=datetime.now(timezone.utc),
                                    updated_at=datetime.now(timezone.utc),
                                ),
                            )
                            # Track the association
                            derived_log_associations.append(
                                (log_event_id, len(new_derived_logs) - 1),
                            )

            # Bulk insert all new derived logs in one go
            session.bulk_save_objects(new_derived_logs, return_defaults=True)
            session.flush()  # Get IDs for the new derived logs

            # Create LogEventDerivedLog associations
            for log_event_id, derived_log_index in derived_log_associations:
                if derived_log_index < len(new_derived_logs):
                    association = LogEventDerivedLog(
                        log_event_id=log_event_id,
                        derived_log_id=new_derived_logs[derived_log_index].id,
                    )
                    session.add(association)

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
            # Use infer_type=True to infer type from value (no explicit_types here)
            field_type_dao.create_field_type_if_absent(
                project_id=project_obj.id,
                field_name=body.key,
                value=non_null_val,
                field_category="derived_entry",
                context_id=context_id,
                infer_type=True,  # Infer type from value for derived entries
            )

        except Exception as e:
            session.rollback()
            raise HTTPException(
                status_code=500,
                detail=f"Failed to create derived logs with key='{body.key}'. Error: {e}",
            )
        return {
            "info": f"Created {len(computed_values)} derived logs with key='{body.key}'.",
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
    # Instantiate DAOs with shared session
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    field_type_dao = FieldTypeDAO(session)
    derived_log_dao = DerivedLogDAO(session)

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

    updated_equation = body.equation if body.equation else None
    updated_key = body.key
    new_refs = body.referenced_logs  # can be None

    # 2) Resolve which DerivedLog IDs to update
    resolved_ids = prepare_resolved_ids(
        equation=updated_equation,
        referenced_logs=body.target_derived_logs,
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
    # NOTE: currently we assume referenced logs are of equal length
    derived_log_ids = [dlog_id for dlog_id in resolved_ids.values()][0]
    existing_derived_logs = (
        session.query(DerivedLog)
        .join(LogEventDerivedLog, LogEventDerivedLog.derived_log_id == DerivedLog.id)
        .filter(
            LogEventDerivedLog.log_event_id.in_(derived_log_ids),
            DerivedLog.key == updated_key,
        )
        .all()
    )
    # If user *did not* pass new referenced_logs, do a simple "update in place"
    if not new_refs:
        # just update existing rows for new key/equation, then recompute
        # bulk update
        try:
            stmt = (
                update(DerivedLog)
                .where(DerivedLog.id.in_([dlog.id for dlog in existing_derived_logs]))
                .values(key=updated_key, equation=updated_equation)
            )
            session.execute(stmt)
            session.commit()
        except ValueError as ve:
            raise HTTPException(status_code=400, detail=str(ve))
        # recompute
        derived_log_dao.recompute_derived_logs(
            logs_to_recompute=existing_derived_logs,
            session=session,
            json_encoder=CustomEncoder,
        )
        return {
            "info": f"Updated {len(existing_derived_logs)} derived logs successfully.",
        }

    # 3) If new_refs *is* provided, do the "compute & insert" approach
    if new_refs:
        # Use updated_key/equation if provided; otherwise, take them from one of the matched logs.
        valid_logs = (
            session.query(DerivedLog)
            .join(
                LogEventDerivedLog,
                LogEventDerivedLog.derived_log_id == DerivedLog.id,
            )
            .filter(
                LogEventDerivedLog.log_event_id.in_(derived_log_ids),
                DerivedLog.key == updated_key,
            )
            .first()
        )
        final_key = updated_key if updated_key else valid_logs.key
        final_equation = updated_equation if updated_equation else valid_logs.equation
        # Delete all derived logs that were matched by the update filter.
        derived_logs_to_delete = (
            session.query(DerivedLog.id)
            .join(
                LogEventDerivedLog,
                LogEventDerivedLog.derived_log_id == DerivedLog.id,
            )
            .filter(
                LogEventDerivedLog.log_event_id.in_(derived_log_ids),
                DerivedLog.key == valid_logs.key,
            )
            .all()
        )
        derived_log_ids_to_delete = [dlog[0] for dlog in derived_logs_to_delete]
        if derived_log_ids_to_delete:
            session.query(DerivedLog).filter(
                DerivedLog.id.in_(derived_log_ids_to_delete),
            ).delete(synchronize_session=False)

        # Also delete the field type records for these derived logs
        field_type_dao.delete_field_type(
            project_id=project_id,
            field_name=valid_logs.key,
            context_id=context_id,
        )
        session.flush()  # flush the deletion so that new insertions do not conflict

        # Resolve the new referenced logs
        new_resolved_ids = prepare_resolved_ids(
            equation=final_equation,
            referenced_logs=new_refs,  # use the new referenced logs
            request_fastapi=request_fastapi,
            project_name=body.project,
            project_dao=project_dao,
            field_type_dao=field_type_dao,
            context_dao=context_dao,
            session=session,
        )
        # NOTE: currently we assume referenced logs are of equal length
        new_derived_log_ids = [dlog_id for dlog_id in new_resolved_ids.values()][0]
        # If none found, short-circuit
        if not any(len(v) for v in new_resolved_ids.values()):
            return {"info": "No references found. Nothing to update."}

        # Get the common length of all resolved ID lists
        lengths = [len(lst) for lst in new_resolved_ids.values()]
        if lengths and len(set(lengths)) != 1:
            raise HTTPException(
                status_code=400,
                detail=f"All referenced log lists must have the same length. Found lengths: {lengths}",
            )

        # Compute derived values directly instead of creating empty logs and recomputing later
        # 1. Substitute placeholders to get filter expression and alias mapping
        filter_expr, alias_to_key_map = _substitute_placeholders(
            final_equation,
            new_resolved_ids,
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
        for key, ids in new_resolved_ids.items():
            resolved_ids_dict.setdefault(alias_to_key_map[key], []).extend(ids)

        # 5. Get the filtered log events for this project
        log_event_ids_subq = (
            session.query(LogEvent.id)
            .filter(project_id == LogEvent.project_id)
            .filter(LogEvent.id.in_(new_derived_log_ids))
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
        derived_log_associations = []  # Track (log_event_id, derived_log_index)
        now = datetime.now(timezone.utc)
        placeholders = _extract_placeholders(body.equation)
        referenced_logs = {
            ph.split(":")[1]: v
            for ph in placeholders
            for k, v in body.referenced_logs.items()
            if k in ph
        }
        # Iterate over the computed values and resolved IDs
        non_null_value = None
        for i, (_, value) in enumerate(computed_values):
            # Get all log IDs involved in this specific computation
            involved_log_ids = list(set(ids[i] for ids in new_resolved_ids.values()))

            # Create a derived entry for each log ID involved in this computation
            for log_event_id in involved_log_ids:
                # Convert value using CustomEncoder for proper JSON serialization
                val = json.loads(json.dumps(value, cls=CustomEncoder))
                non_null_val = val if val is not None else non_null_value
                inferred_type = LogDAO.infer_type("", non_null_val)

                # Create DerivedLog without log_event_id
                new_derived_logs.append(
                    DerivedLog(
                        key=final_key,
                        equation=final_equation,
                        referenced_logs=referenced_logs,
                        value=non_null_val,
                        inferred_type=inferred_type,
                        created_at=now,
                        updated_at=now,
                    ),
                )
                # Track the association
                derived_log_associations.append(
                    (log_event_id, len(new_derived_logs) - 1),
                )

        # Bulk insert all new derived logs in one go
        session.bulk_save_objects(new_derived_logs, return_defaults=True)
        session.flush()  # Get IDs for the new derived logs

        # Create LogEventDerivedLog associations
        for log_event_id, derived_log_index in derived_log_associations:
            if derived_log_index < len(new_derived_logs):
                association = LogEventDerivedLog(
                    log_event_id=log_event_id,
                    derived_log_id=new_derived_logs[derived_log_index].id,
                )
                session.add(association)

        session.commit()

        # Update the field type record for the derived entry
        # Use infer_type=True to infer type from value (no explicit_types here)
        field_type_dao.create_field_type_if_absent(
            project_id=project_id,
            context_id=context_id,
            field_name=final_key,
            mutable=True,
            value=non_null_val,
            field_category="derived_entry",
            infer_type=True,  # Infer type from value for derived entries
        )

        return {
            "info": f"Updated references and replaced {len(new_derived_logs)} old derived logs with {len(new_derived_logs)} new ones.",
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
                        "detail": "When passing a filter dict in `logs`, you must supply `project` or `context`.",
                    },
                },
            },
        },
    },
)
def update_logs(
    request_fastapi: Request,
    body: UpdateLogRequest,
    session=Depends(get_db_session),
):
    """
    Updates multiple logs with the provided entries. Each entry will be either added
    or overridden in the specified logs.

    The `logs` parameter can be either:
    - A list of log IDs to update
    - A filter dictionary to select logs matching specific criteria (requires `project` or `context`)

    A dictionary of "explicit_types" can be passed as part of the `entries`.
    If present, it will override the inferred type of any matching key in all logs.
    """
    # Instantiate DAOs with shared session
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    field_type_dao = FieldTypeDAO(session)
    log_event_dao = LogEventDAO(session)
    log_dao = LogDAO(session, context_dao)
    derived_log_dao = DerivedLogDAO(session)

    # Get user ID for permission checks
    user_id = request_fastapi.state.user_id

    # Normalize the logs parameter to get IDs to update
    ids_to_update = []

    # Use body.logs to determine which logs to update
    if hasattr(body, "logs") and body.logs is not None:
        # Check if it's a filter dict and validate required fields
        if isinstance(body.logs, dict):
            if not body.project:
                raise HTTPException(
                    status_code=400,
                    detail="When passing a filter dict in `logs`, you must supply `project`.",
                )

            # Get project ID first for filtering
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

            # It's a filter dict, use log_dao.get_ids_by_filter to get matching IDs
            try:
                # Get context ID if provided
                context_ids = None
                if body.context:
                    if isinstance(body.context, str):
                        ctx = context_dao.filter(
                            project_id=project_id,
                            name=body.context,
                        )
                        if ctx:
                            context_ids = [ctx[0][0].id]
                    elif isinstance(body.context, list):
                        context_ids = []
                        for ctx_name in body.context:
                            if isinstance(ctx_name, str):
                                ctx = context_dao.filter(
                                    project_id=project_id,
                                    name=ctx_name,
                                )
                                if ctx:
                                    context_ids.append(ctx[0][0].id)
                else:
                    # get the default context
                    context_ids = [context_dao.get_or_create(project_id, name="")]
                # Use log_dao.get_ids_by_filter to get matching log IDs
                ids_to_update = log_dao.get_ids_by_filter(
                    project_id=project_id,
                    filters=body.logs,
                    context_ids=context_ids,
                )

                if not ids_to_update:
                    # No matching logs found
                    raise HTTPException(
                        status_code=404,
                        detail="No logs found matching the provided filter criteria.",
                    )
            except ValueError as e:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid filter criteria: {str(e)}",
                )
        else:
            # Assume it's a list of IDs
            ids_to_update = body.logs
    else:
        raise HTTPException(
            status_code=400,
            detail="The 'logs' parameter is required and must be either a list of log IDs or a filter dictionary.",
        )

    # Validate all log IDs upfront
    not_found_logs = []
    log_id_to_project = {}  # Maps log_id -> project_id
    updated_ids = set()

    for log_id in ids_to_update:
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

    # Check RESTRICT constraints before update
    if ctx_id:
        from .fk_utils import format_fk_violation_error

        # Determine which columns are being updated
        columns_being_updated = set()
        if body.entries:
            if isinstance(body.entries, dict):
                columns_being_updated.update(body.entries.keys())
            elif isinstance(body.entries, list) and body.entries:
                # Take union of all keys from all entries
                for entry in body.entries:
                    if isinstance(entry, dict):
                        columns_being_updated.update(entry.keys())

        if body.params:
            if isinstance(body.params, dict):
                columns_being_updated.update(body.params.keys())
            elif isinstance(body.params, list) and body.params:
                for param in body.params:
                    if isinstance(param, dict):
                        columns_being_updated.update(param.keys())

        # Remove metadata keys
        columns_being_updated.discard("explicit_types")

        if columns_being_updated:
            # Get OLD values for these columns from logs being updated
            from orchestra.db.models.orchestra_models import Log, LogEventLog

            columns_values_map = {}  # {column_name: [old_values...]}

            for log_id in ids_to_update:
                # Query current log values for columns being updated
                old_values_query = (
                    session.query(Log.key, Log.value)
                    .join(LogEventLog, LogEventLog.log_id == Log.id)
                    .filter(
                        LogEventLog.log_event_id == log_id,
                        Log.key.in_(columns_being_updated),
                    )
                )

                for key, value in old_values_query.all():
                    if value is not None:
                        columns_values_map.setdefault(key, []).append(value)

            # Check for RESTRICT constraint violations
            if columns_values_map:
                violations = context_dao.check_restrict_constraints(
                    project_id=project_id,
                    context_id=ctx_id,
                    columns_values=columns_values_map,
                    action="UPDATE",
                )

                if violations:
                    error_msg = format_fk_violation_error(violations)
                    raise HTTPException(status_code=400, detail=error_msg)

                # Apply CASCADE, SET NULL, and SET DEFAULT actions
                # Extract new values for CASCADE UPDATE
                new_values = {}
                if body.entries:
                    if isinstance(body.entries, dict):
                        new_values.update(body.entries)

                context_dao.apply_fk_actions(
                    project_id=project_id,
                    context_id=ctx_id,
                    columns_values=columns_values_map,
                    action="UPDATE",
                    new_values=new_values,
                )

    # Prepare collections for bulk operations
    all_flat_updates = []
    # Create a separate list for nested updates (using dot or bracket notation)
    all_nested_updates = []
    new_field_types = []
    updates_by_log_id = {}  # For context versioning
    updated_entry_keys = set()  # Track which entry keys are being updated
    failed_updates: list[
        dict
    ] = []  # Collect per-log failures without aborting the batch

    # Process both params and entries
    for data_type in ("params", "entries"):
        data = getattr(body, data_type)

        for i, log_id in enumerate(ids_to_update):
            # Extract the data for this log. Support both dict and list formats.
            try:
                this_data = data if isinstance(data, dict) else data[i]
            except IndexError:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Mismatch between number of log ids ({len(ids_to_update)}) and length of "
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
            flat_data = {}

            # First pass: separate nested updates from flat updates
            for k, v in this_data.items():
                # Check if this is a nested path (contains dots or brackets)
                if "." in k or "[" in k:
                    # Extract base key and path segments
                    parts = k.split(".", 1) if "." in k else k.split("[", 1)
                    base_key = parts[0]
                    path_segments = k[len(base_key) :]  # Everything after the base key

                    # Process nested field update with type enforcement
                    try:
                        field_result = log_dao.check_field_update(
                            field_key=base_key,
                            field_types=field_types,
                            explicit_types_dict=explicit_types,
                            is_nested=True,
                        )
                    except ValueError as e:
                        failed_updates.append(
                            {
                                "log_event_id": log_id,
                                "error": f"{str(e)} (in batch entry {i})",
                            },
                        )
                        continue

                    # ToDo: Need to `enforce_types` here by merging the partial update with the
                    # existing value and then enforcing/checking if the full updated value satsifies
                    # the type constraints or not

                    # If field doesn't exist, create it
                    if not field_result["exists"]:
                        category = "entry" if data_type == "entries" else "param"
                        new_field_types.append(
                            {
                                "project_id": project_id,
                                "field_name": base_key,
                                "value": v,
                                "mutable": field_result["mutable"],
                                "unique": field_result["unique"],
                                "field_category": category,
                                "context_id": ctx_id,
                                "field_type": field_result["field_type"],
                                "enum_values": field_result["enum_values"],
                                "enum_restrict": field_result["enum_restrict"],
                            },
                        )

                    # Add to nested updates
                    all_nested_updates.append(
                        {
                            "log_event_id": log_id,
                            "base_key": base_key,
                            "path_segments": path_segments,
                            "new_value": v,
                            "context_id": ctx_id if "ctx_ids" not in locals() else None,
                            "context_ids": ctx_ids if "ctx_ids" in locals() else None,
                            "overwrite": body.overwrite,
                            "explicit_types": explicit_types,
                        },
                    )

                    # Track this update for context versioning
                    updated_ids.add((base_key, log_id))
                    if data_type == "entries":
                        updated_entry_keys.add(base_key)
                else:
                    # This is a flat update, keep it for normal processing
                    flat_data[k] = v
                    if data_type == "entries":
                        updated_entry_keys.add(k)

            # Process flat updates normally
            for k, v in flat_data.items():
                # Process flat field update with type enforcement
                try:
                    field_result = log_dao.check_field_update(
                        field_key=k,
                        field_types=field_types,
                        explicit_types_dict=explicit_types,
                        is_nested=False,
                    )
                except ValueError as e:
                    failed_updates.append(
                        {
                            "log_event_id": log_id,
                            "error": f"{str(e)} (in batch entry {i})",
                        },
                    )
                    continue

                # Enforce types if field exists
                if field_result["exists"]:
                    from orchestra.web.api.log.utils.logging_utils import enforce_types

                    try:
                        enforce_types(
                            k,
                            v,
                            field_types=field_types,
                            field_type_dao=field_type_dao,
                            context_dao=context_dao,
                            project_id=project_id,
                            batch_index=i,
                            explicit_types=explicit_types,
                            context_id=ctx_id,
                            is_param=(data_type == "params"),
                        )
                    except HTTPException as e:
                        failed_updates.append(
                            {
                                "log_event_id": log_id,
                                "error": getattr(e, "detail", str(e)),
                            },
                        )
                        continue

                # If field doesn't exist, create it
                if not field_result["exists"]:
                    category = "entry" if data_type == "entries" else "param"
                    new_field_types.append(
                        {
                            "project_id": project_id,
                            "field_name": k,
                            "value": v,
                            "mutable": field_result["mutable"],
                            "unique": field_result["unique"],
                            "field_category": category,
                            "context_id": ctx_id,
                            "field_type": field_result["field_type"],
                            "enum_values": field_result["enum_values"],
                            "enum_restrict": field_result["enum_restrict"],
                        },
                    )

                # Compute the version based on whether we're handling params or entries.
                param_version = None
                if data_type == "params":
                    existing = log_dao.filter(
                        key=k,
                        value=json.dumps(v),
                        project_id=project_id,
                    )
                    if existing:
                        param_version = existing[0][0].param_version
                    else:
                        param_version = log_dao.get_next_param_version(
                            project_id,
                            ctx_id,
                            k,
                        )

                # Add to the batch update list
                # If we have multiple contexts, create an update for each context
                if "ctx_ids" in locals() and ctx_ids:
                    for context_id in ctx_ids:
                        all_flat_updates.append(
                            {
                                "log_event_id": log_id,
                                "key": k,
                                "value": v,
                                "param_version": param_version,
                                "explicit_types": explicit_types,
                                "field_types": field_types,
                                "context_id": context_id,
                                "project_id": project_id,
                                "overwrite": body.overwrite,
                            },
                        )
                else:
                    all_flat_updates.append(
                        {
                            "log_event_id": log_id,
                            "key": k,
                            "value": v,
                            "param_version": param_version,
                            "explicit_types": explicit_types,
                            "field_types": field_types,
                            "context_id": ctx_id,
                            "project_id": project_id,
                            "overwrite": body.overwrite,
                        },
                    )
                updated_ids.add((k, log_id))

    # Bulk create any new field types
    if new_field_types:
        field_type_dao.bulk_create_field_types(new_field_types)

    successful_update_ids: set[int] = set()

    # First, handle flat updates
    if all_flat_updates:
        try:
            # Call bulk_update once with all updates
            bulk_result = log_dao.bulk_update(
                all_flat_updates,
                field_types=field_types,
                overwrite=body.overwrite,
            )

            # Add bulk_update failures to our failed_updates list
            failed_updates.extend(bulk_result["failed"])

            # For each successful ID from bulk_update, check for duplicates
            for le_id in bulk_result["successful_update_ids"]:
                duplicate_found = False
                if "ctx_ids" in locals() and ctx_ids:
                    for context_id in ctx_ids:
                        if context_id is not None:
                            ctx_obj = (
                                context_dao.session.query(Context)
                                .filter_by(id=context_id)
                                .first()
                            )
                            if ctx_obj and not ctx_obj.allow_duplicates:
                                duplicate = context_dao.check_for_duplicates_subset(
                                    context_id=context_id,
                                    log_event_id=le_id,
                                    keys_to_check=list(updated_entry_keys),
                                )
                                if duplicate:
                                    failed_updates.append(
                                        {
                                            "log_event_id": le_id,
                                            "error": f"Duplicate log entry detected in context '{ctx_obj.name}'",
                                        },
                                    )
                                    duplicate_found = True
                                    break
                elif ctx_id is not None:
                    ctx_obj = (
                        context_dao.session.query(Context).filter_by(id=ctx_id).first()
                    )
                    if ctx_obj and not ctx_obj.allow_duplicates:
                        duplicate = context_dao.check_for_duplicates_subset(
                            context_id=ctx_id,
                            log_event_id=le_id,
                            keys_to_check=list(updated_entry_keys),
                        )
                        if duplicate:
                            failed_updates.append(
                                {
                                    "log_event_id": le_id,
                                    "error": f"Duplicate log entry detected in context '{ctx_obj.name}'",
                                },
                            )
                            duplicate_found = True

                if not duplicate_found:
                    successful_update_ids.add(le_id)
        except ValueError as e:
            detail = e.detail if isinstance(e, HTTPException) else str(e)
            failed_updates.append(
                {
                    "log_event_id": le_id,
                    "error": f"Found differing log param value with the same version: {detail}",
                },
            )
        except OverwriteError as e:
            detail = e.detail if isinstance(e, HTTPException) else str(e)
            failed_updates.append(
                {
                    "log_event_id": le_id,
                    "error": f"Existing value cannot be overwritten because overwrite is set to False: {detail}",
                },
            )
        except ImmutableFieldError as e:
            detail = e.detail if isinstance(e, HTTPException) else str(e)
            failed_updates.append(
                {
                    "log_event_id": le_id,
                    "error": f"Field is immutable and cannot be modified: {detail}",
                },
            )

    # Then, handle nested updates if any exist (grouped per log/key for partial success)
    if all_nested_updates:
        # Group by (log_event_id, base_key)
        ngroups: dict[tuple[int, str], list[dict]] = {}
        for patch in all_nested_updates:
            le_id = patch.get("log_event_id")
            base_key = patch.get("base_key")
            if le_id is None or base_key is None:
                continue
            ngroups.setdefault((le_id, base_key), []).append(patch)

        for (le_id, _base), group in ngroups.items():
            try:
                log_dao.apply_jsonb_patch(
                    group,
                    field_types=field_types,
                )
                # Inline duplicate checks for this log id; only mark success if it passes
                duplicate_found = False
                if "ctx_ids" in locals() and ctx_ids:
                    for context_id in ctx_ids:
                        if context_id is not None:
                            ctx_obj = (
                                context_dao.session.query(Context)
                                .filter_by(id=context_id)
                                .first()
                            )
                            if ctx_obj and not ctx_obj.allow_duplicates:
                                duplicate = context_dao.check_for_duplicates_subset(
                                    context_id=context_id,
                                    log_event_id=le_id,
                                    keys_to_check=list(updated_entry_keys),
                                )
                                if duplicate:
                                    failed_updates.append(
                                        {
                                            "log_event_id": le_id,
                                            "error": f"Duplicate log entry detected in context '{ctx_obj.name}'",
                                        },
                                    )
                                    duplicate_found = True
                elif ctx_id is not None:
                    ctx_obj = (
                        context_dao.session.query(Context).filter_by(id=ctx_id).first()
                    )
                    if ctx_obj and not ctx_obj.allow_duplicates:
                        duplicate = context_dao.check_for_duplicates_subset(
                            context_id=ctx_id,
                            log_event_id=le_id,
                            keys_to_check=list(updated_entry_keys),
                        )
                        if duplicate:
                            failed_updates.append(
                                {
                                    "log_event_id": le_id,
                                    "error": f"Duplicate log entry detected in context '{ctx_obj.name}'",
                                },
                            )
                            duplicate_found = True

                if not duplicate_found:
                    successful_update_ids.add(le_id)
            except ValueError as e:
                detail = e.detail if isinstance(e, HTTPException) else str(e)
                failed_updates.append(
                    {
                        "log_event_id": le_id,
                        "error": f"Error applying nested updates: {detail}",
                    },
                )
            except OverwriteError as e:
                detail = e.detail if isinstance(e, HTTPException) else str(e)
                failed_updates.append(
                    {
                        "log_event_id": le_id,
                        "error": f"Existing nested value cannot be overwritten because overwrite is set to False: {detail}",
                    },
                )
            except ImmutableFieldError as e:
                detail = e.detail if isinstance(e, HTTPException) else str(e)
                failed_updates.append(
                    {
                        "log_event_id": le_id,
                        "error": f"Field or nested path is immutable and cannot be modified: {detail}",
                    },
                )
            except (IndexError, Exception) as e:
                detail = e.detail if isinstance(e, HTTPException) else str(e)
                failed_updates.append({"log_event_id": le_id, "error": detail})

    # Update context version if needed
    if "ctx_ids" in locals() and ctx_ids:
        # Handle multiple contexts if we have a list
        for context_id in ctx_ids:
            if context_id is not None:
                ctx_obj = (
                    context_dao.session.query(Context).filter_by(id=context_id).first()
                )
                if ctx_obj and ctx_obj.is_versioned and updates_by_log_id:
                    ctx_obj.updated_at = datetime.now(timezone.utc)
        # Commit all changes at once
        if updates_by_log_id:
            context_dao.session.commit()
    elif ctx_id is not None:
        # Original single context behavior
        ctx_obj = context_dao.session.query(Context).filter_by(id=ctx_id).first()
        if ctx_obj and ctx_obj.is_versioned and updates_by_log_id:
            ctx_obj.updated_at = datetime.now(timezone.utc)
            context_dao.session.commit()

    # Final sanity: if everything failed, surface an error instead of returning 200
    if not successful_update_ids and failed_updates:
        first_error = failed_updates[0].get("error", "Update failed")
        raise HTTPException(status_code=400, detail=first_error)

    # Recompute derived logs that reference any updated base logs (only successes).
    if updated_ids:
        updated_ids = {
            (k, le_id) for (k, le_id) in updated_ids if le_id in successful_update_ids
        }
        try:
            event_ids = [value for (_, value) in updated_ids]
            derived_logs_to_recompute = (
                session.query(DerivedLog)
                .join(
                    LogEventDerivedLog,
                    LogEventDerivedLog.derived_log_id == DerivedLog.id,
                )
                .join(LogEvent, LogEvent.id == LogEventDerivedLog.log_event_id)
                .filter(
                    LogEvent.project_id == project_id,
                    LogEventDerivedLog.log_event_id.in_(event_ids),
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

    return {"info": "Logs updated successfully!", "failed": failed_updates}


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
    # Instantiate DAOs with shared session
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    field_type_dao = FieldTypeDAO(session)
    log_event_dao = LogEventDAO(session)
    log_dao = LogDAO(session, context_dao)

    if body.source_type not in ("all", "base", "derived"):
        raise HTTPException(
            status_code=400,
            detail="source_type must be one of: 'all', 'base', 'derived'",
        )

    not_found_logs = []
    not_found_entries = []
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

    # Preprocess ids_and_fields to handle dict-based selectors
    processed_ids_and_fields = []
    for id_spec, fields in body.ids_and_fields:
        if isinstance(id_spec, dict):
            try:
                # Use log_dao.get_ids_by_filter to get matching log IDs
                matching_ids = log_dao.get_ids_by_filter(
                    project_id=project_id,
                    filters=id_spec,
                    context_ids=[context_id] if context_id else None,
                )

                # Add each ID with the same fields to the processed list
                for log_id in matching_ids:
                    processed_ids_and_fields.append((log_id, fields))
            except ValueError as e:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid filter criteria: {str(e)}",
                )
        else:
            # Pass through unchanged if it's not a dict
            processed_ids_and_fields.append((id_spec, fields))

    # Use the processed list instead of the original
    ids_and_fields = _flatten_fields(processed_ids_and_fields)

    # Check RESTRICT constraints before deletion
    if body.source_type in ("all", "base"):
        from orchestra.db.models.orchestra_models import (
            Log,
            LogEvent,
            LogEventContext,
            LogEventLog,
        )

        from .fk_utils import format_fk_violation_error

        # Extract column-value pairs being deleted
        columns_values_to_delete = {}  # {column_name: [values...]}

        # Handle global field deletions (log_id is None)
        global_fields = ids_and_fields.get(None, [])
        if global_fields:
            # Get all values for these fields in the context
            for field in global_fields:
                values_query = (
                    session.query(Log.value)
                    .join(LogEventLog, LogEventLog.log_id == Log.id)
                    .join(LogEvent, LogEvent.id == LogEventLog.log_event_id)
                    .join(LogEventContext, LogEventContext.log_event_id == LogEvent.id)
                    .filter(
                        LogEvent.project_id == project_id,
                        LogEventContext.context_id == context_id,
                        Log.key == field,
                    )
                    .distinct()
                )
                values = [row[0] for row in values_query.all() if row[0] is not None]
                if values:
                    columns_values_to_delete[field] = values

        # Handle specific log deletions
        specific_log_ids = [k for k in ids_and_fields.keys() if k is not None]
        if specific_log_ids:
            # Group fields by log_event_id for efficiency
            for log_id, fields in ids_and_fields.items():
                if log_id is None:
                    continue

                # If no fields specified, we're deleting the entire log event
                if not fields:
                    # Get all fields for this log event
                    all_fields_query = (
                        session.query(Log.key, Log.value)
                        .join(LogEventLog, LogEventLog.log_id == Log.id)
                        .filter(LogEventLog.log_event_id == log_id)
                    )
                    for key, value in all_fields_query.all():
                        if value is not None:
                            columns_values_to_delete.setdefault(key, []).append(value)
                else:
                    # Get values for specific fields
                    fields_values_query = (
                        session.query(Log.key, Log.value)
                        .join(LogEventLog, LogEventLog.log_id == Log.id)
                        .filter(
                            LogEventLog.log_event_id == log_id,
                            Log.key.in_(fields),
                        )
                    )
                    for key, value in fields_values_query.all():
                        if value is not None:
                            columns_values_to_delete.setdefault(key, []).append(value)

        # Check for RESTRICT constraint violations
        if columns_values_to_delete:
            violations = context_dao.check_restrict_constraints(
                project_id=project_id,
                context_id=context_id,
                columns_values=columns_values_to_delete,
                action="DELETE",
            )

            if violations:
                error_msg = format_fk_violation_error(violations)
                raise HTTPException(status_code=400, detail=error_msg)

            # Apply CASCADE, SET NULL, and SET DEFAULT actions
            context_dao.apply_fk_actions(
                project_id=project_id,
                context_id=context_id,
                columns_values=columns_values_to_delete,
                action="DELETE",
            )

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
            # Find log IDs to delete using a subquery
            log_ids_to_delete = (
                session.query(Log.id)
                .join(
                    LogEventLog,
                    LogEventLog.log_id == Log.id,
                )
                .filter(
                    LogEventLog.log_event_id.in_(all_log_events_subq),
                    Log.key.in_(fields),
                )
                .subquery()
            )

            # Delete GCS files BEFORE deleting DB records
            deletion_query = session.query(Log).filter(
                Log.id.in_(select(log_ids_to_delete)),
            )
            log_dao._bulk_delete_gcs_media(deletion_query)

            # Now delete the logs without joins
            deleted_count = (
                session.query(Log)
                .filter(
                    Log.id.in_(select(log_ids_to_delete)),
                )
                .delete(synchronize_session=False)
            )
            if deleted_count > 0:
                context_description.append(
                    f"Deleted {len(fields)} fields from {deleted_count} base logs",
                )

        # Bulk delete from derived logs with a single query
        if body.source_type in ("all", "derived"):
            # Use a single DELETE statement for all fields
            # Find derived logs to delete
            derived_logs_to_delete = (
                session.query(DerivedLog.id)
                .join(
                    LogEventDerivedLog,
                    LogEventDerivedLog.derived_log_id == DerivedLog.id,
                )
                .filter(
                    LogEventDerivedLog.log_event_id.in_(all_log_events_subq),
                    DerivedLog.key.in_(fields),
                )
                .all()
            )
            derived_log_ids_to_delete = [dlog[0] for dlog in derived_logs_to_delete]
            deleted_count = 0
            if derived_log_ids_to_delete:
                deleted_count = (
                    session.query(DerivedLog)
                    .filter(DerivedLog.id.in_(derived_log_ids_to_delete))
                    .delete(synchronize_session=False)
                )
            if deleted_count > 0:
                context_description.append(
                    f"Deleted {len(fields)} fields from {deleted_count} derived logs",
                )

        # Mark that we need to update the context
        if context_description:
            context_updated = True

    # Group 2: Entire log event deletions (fields is empty i.e. passed in as None)
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

        # Get all field types and add to deleted_fields set
        deleted_fields.update(
            field_type_dao.get_field_types(
                project_id,
                context_id=context_id,
                return_mutable=True,
            ).keys(),
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

        # Delete GCS files BEFORE deleting DB records
        # Build a query to find logs by their log_event_id and key
        logs_to_delete_query = session.query(Log).join(
            LogEventLog,
            LogEventLog.log_id == Log.id,
        )

        # Build filter for each (event_id, key) pair
        combined_filter = or_(
            *[
                and_(LogEventLog.log_event_id == eid, Log.key == k)
                for eid, k in base_log_deletions
            ],
        )
        logs_to_delete_query = logs_to_delete_query.filter(combined_filter)
        log_dao._bulk_delete_gcs_media(logs_to_delete_query)

        # Group by key for more efficient deletion
        key_to_event_ids = defaultdict(list)
        for event_id, key in base_log_deletions:
            key_to_event_ids[key].append(event_id)

        for key, event_ids in key_to_event_ids.items():
            try:
                # First find the logs to delete
                logs_to_delete = (
                    session.query(Log.id)
                    .join(LogEventLog, LogEventLog.log_id == Log.id)
                    .filter(
                        Log.key == key,
                        LogEventLog.log_event_id.in_(event_ids),
                    )
                    .all()
                )

                # Delete them if found
                if logs_to_delete:
                    log_ids_to_delete = [log_id[0] for log_id in logs_to_delete]
                    deleted_count = (
                        session.query(Log)
                        .filter(Log.id.in_(log_ids_to_delete))
                        .delete(synchronize_session=False)
                    )
                else:
                    deleted_count = 0
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
                # Find derived logs to delete
                derived_logs_to_delete = (
                    session.query(DerivedLog.id)
                    .join(
                        LogEventDerivedLog,
                        LogEventDerivedLog.derived_log_id == DerivedLog.id,
                    )
                    .filter(
                        DerivedLog.key == key,
                        LogEventDerivedLog.log_event_id.in_(event_ids),
                    )
                    .all()
                )
                derived_log_ids_to_delete = [dlog[0] for dlog in derived_logs_to_delete]
                if derived_log_ids_to_delete:
                    deleted_count = (
                        session.query(DerivedLog)
                        .filter(DerivedLog.id.in_(derived_log_ids_to_delete))
                        .delete(synchronize_session=False)
                    )
                else:
                    deleted_count = 0
            except:
                not_found_entries.append((event_ids, key))
                continue

            if deleted_count > 0:
                context_description.append(
                    f"Deleted field '{key}' from {deleted_count} derived logs",
                )

    # Delete empty log events if requested
    if body.delete_empty_logs and potential_empty_logs:
        # Get all log_event_ids that still have logs in a single query
        still_used_base_ids = set(
            row[0]
            for row in session.query(LogEventLog.log_event_id)
            .join(Log, Log.id == LogEventLog.log_id)
            .filter(LogEventLog.log_event_id.in_(potential_empty_logs))
            .distinct()
        )

        still_used_derived_ids = set(
            row[0]
            for row in session.query(LogEventDerivedLog.log_event_id)
            .join(DerivedLog, DerivedLog.id == LogEventDerivedLog.derived_log_id)
            .filter(LogEventDerivedLog.log_event_id.in_(potential_empty_logs))
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
    if deleted_fields and body.delete_empty_fields:
        # Get all fields that still exist in any logs with two efficient queries
        existing_base_fields = (
            session.query(Log.key)
            .join(LogEventLog, LogEventLog.log_id == Log.id)
            .join(LogEvent, LogEvent.id == LogEventLog.log_event_id)
            .filter(LogEvent.project_id == project_id)
            .distinct()
            .all()
        )
        existing_derived_fields = (
            session.query(DerivedLog.key)
            .join(
                LogEventDerivedLog,
                LogEventDerivedLog.derived_log_id == DerivedLog.id,
            )
            .join(LogEvent, LogEvent.id == LogEventDerivedLog.log_event_id)
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
        description='JSON-encoded dict mapping either static column names (e.g. `timestamp`) or full Python2SQL expressions (e.g. `cosine(embed(\'search text\'), embedding_vector)`) to sort directions (`"ascending"` or `"descending"`). The first key is the primary sort field; subsequent keys break ties.',
        example={"timestamp": "descending", "round(score, 2)": "ascending"},
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
    limit: Optional[int] = Query(None, ge=1, le=1000),
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
    return_ids_only: bool = Query(
        False,
        description="If True, return only log IDs instead of full entries.",
    ),
    randomize: bool = Query(
        False,
        description="If true, return logs in a deterministic random order (fixed seed) instead of newest-first.",
    ),
    seed: Optional[str] = Query(
        "42",
        description="If provided, use this seed for deterministic random ordering instead of the default.",
    ),
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
           to either lists of log ids (if return_timestamps is False) or mappings of `{log id: timestamp}` (if True).

      3. **Return IDs only mode**:
         - If return_ids_only is True, returns only the log event ids.

      4. **Dynamic expression sorting**:
         - In addition to static field-based sorting, you can use dynamic expressions for sorting.
         - The same grammar supported for `filter_expr` applies to sorting expressions.

    The response always includes:
      - `params`: The parameter versions used across the logs.
      - `count`: The total number of logs matching the query.
      - Additionally, it includes either `logs` (in monolithic or nested grouping mode) or `groups` (in flat grouping mode)
        as specified by the arguments.

    """
    # Instantiate DAOs with shared session
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    field_type_dao = FieldTypeDAO(session)

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
            randomize=randomize,
            seed=seed,
        )
        if return_ids_only:
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
    # Stage 5: Return the Final Result.
    # -----------------------------------------------------------
    return final_result


@router.post(
    "/logs/query",
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
                        ],
                        "count": 1,
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
def query_logs_post(
    request_fastapi: Request,
    body: QueryLogsPostBody = Body(...),
    session=Depends(get_db_session),
):
    """
    Query logs via POST request.

    This endpoint accepts the exact same parameters as GET /logs, but via request body
    instead of query parameters. This is useful for:
    - Large filter expressions that would exceed URL length limits
    - Filter expressions containing base64-encoded images (e.g., embed_image('data:image/png;base64,...'))
    - Complex sorting expressions

    Example with image embedding:
    ```json
    {
        "project": "my-project",
        "filter_expr": "cosine(image_embedding, embed_image('data:image/png;base64,iVBORw0KG...')) < 0.3",
        "limit": 10
    }
    ```
    """
    # Instantiate DAOs with shared session
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    field_type_dao = FieldTypeDAO(session)

    # Validate project
    try:
        project_id = project_dao.get_by_user_and_name(
            name=body.project,
            user_id=request_fastapi.state.user_id,
        ).id
    except Exception as e:
        raise HTTPException(
            status_code=404,
            detail=f"Project {body.project} not found.",
        )

    # Format logs into flat structure.
    context_name = "" if not body.context else body.context
    context_obj = context_dao.filter(name=context_name, project_id=project_id)
    if context_obj:
        context_id = context_obj[0][0].id
    else:
        context_id = None

    # Handle non-grouped case (same as GET /logs)
    if not body.group_by:
        all_rows, context_len, total_count = _get_logs_query(
            request_fastapi,
            project=body.project,
            column_context=body.column_context,
            context=body.context,
            filter_expr=body.filter_expr,
            sorting=body.sorting,
            from_ids=body.from_ids,
            exclude_ids=body.exclude_ids,
            from_fields=body.from_fields,
            exclude_fields=body.exclude_fields,
            limit=body.limit,
            offset=body.offset,
            project_dao=project_dao,
            field_type_dao=field_type_dao,
            context_dao=context_dao,
            session=session,
            randomize=body.randomize,
            seed=body.seed,
        )

        # Get field order
        field_order_map = field_type_dao.get_ordered_field_names(
            project_id=project_id,
            context_id=context_id,
        )

        # Format logs
        logs_out, params_out = _format_flat_logs(
            all_rows,
            context_len,
            body.value_limit,
            field_order_map,
        )

        # Apply group threshold if needed
        if body.group_threshold:
            logs_out = apply_group_threshold(logs_out, body.group_threshold)

        response = {
            "params": params_out,
            "logs": logs_out,
            "count": total_count,
        }

        # Return IDs only if requested
        if body.return_ids_only:
            response["logs"] = [log["id"] for log in logs_out]

        return response
    else:
        # Handle grouped case - similar to GET /logs grouped logic
        all_rows, context_len, total_count = _get_all_filtered_log_event_ids(
            request_fastapi=request_fastapi,
            project=body.project,
            column_context=body.column_context,
            context=body.context,
            filter_expr=body.filter_expr,
            sorting=body.sorting,
            from_ids=body.from_ids,
            exclude_ids=body.exclude_ids,
            from_fields=body.from_fields,
            exclude_fields=body.exclude_fields,
            project_dao=project_dao,
            field_type_dao=field_type_dao,
            context_dao=context_dao,
            session=session,
            randomize=body.randomize,
            seed=body.seed,
        )

        # Build grouped structure
        grouped_result = _build_grouped_data(
            group_by=body.group_by,
            all_log_event_ids=all_rows,
            request_fastapi=request_fastapi,
            project=body.project,
            column_context=body.column_context,
            context=body.context,
            filter_expr=body.filter_expr,
            sorting=body.sorting,
            group_sorting=body.group_sorting,
            from_ids=body.from_ids,
            exclude_ids=body.exclude_ids,
            from_fields=body.from_fields,
            exclude_fields=body.exclude_fields,
            limit=body.limit,
            offset=body.offset,
            group_limit=body.group_limit,
            group_offset=body.group_offset,
            group_depth=body.group_depth,
            nested_groups=body.nested_groups,
            groups_only=body.groups_only,
            return_timestamps=body.return_timestamps,
            return_ids_only=body.return_ids_only,
            value_limit=body.value_limit,
            project_dao=project_dao,
            field_type_dao=field_type_dao,
            context_dao=context_dao,
            session=session,
            project_id=project_id,
            context_id=context_id,
        )

        return grouped_result


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
    limit: Optional[int] = Query(None, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    session=Depends(get_db_session),
    randomize: bool = Query(
        False,
        description="If true, return logs in a deterministic random order (fixed seed) instead of newest-first.",
    ),
):
    """
    Returns the update timestamp of the most recently updated log within the specified
    page and filter bounds.
    """
    # Instantiate DAOs with shared session
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    field_type_dao = FieldTypeDAO(session)

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
        randomize=randomize,
    )


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
    session=Depends(get_db_session),
) -> Union[Dict[str, Any], float, int, bool, str, None]:
    """
    Returns the reduction metric for filtered values (base + derived) for one or more keys from a project.

    This endpoint supports three modes of operation:

    1. Single key, no grouping: Returns a single metric value
       Example:
       ```bash
       GET /logs/metric/mean?key=score
       ```
       Response:
       ```json
       4.56
       ```

    2. Multiple keys, no grouping: Returns a dict mapping keys to metric values
       Example:
       ```bash
       GET /logs/metric/mean?key=["score","length"]
       ```
       Response:
       ```json
       {"score": 4.56, "length": 120}
       ```

    3. With grouping: Returns metrics grouped by one or more fields
       Example:
       ```bash
       GET /logs/metric/mean with body {"key": "score", "group_by": "model"}
       ```
       Response:
       ```json
       {"gpt-4": 4.56, "gpt-3.5": 3.78}
       ```

       For nested grouping, provide a list of fields:
       Example:
       ```bash
       GET /logs/metric/mean with body {"key": "score", "group_by": ["model", "temperature"]}
       ```
       Response:
       ```json
       {"gpt-4": {"0.7": 4.56, "0.9": 4.23}, "gpt-3.5": {"0.7": 3.78, "0.9": 3.45}}
       ```

    The group_by parameter can be a string for single-level grouping or a list of strings for
    nested grouping. Each group_by field can be prefixed with "params/" to indicate it's a parameter.
    """
    # Instantiate DAOs with shared session
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    field_type_dao = FieldTypeDAO(session)

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
        all_metrics = request.metrics or {}
        # Check if all keys use the same metric for bulk computation
        metrics_per_key = {k: (all_metrics.get(k, default_metric)) for k in all_keys}
        unique_metrics = set(metrics_per_key.values())

        # If all keys use the same metric, use bulk computation
        if len(unique_metrics) == 1:
            common_metric = next(iter(unique_metrics))

            # Handle key-specific filters
            has_key_specific_filters = False
            for k in all_keys:
                (
                    key_filter_expr,
                    key_from_ids,
                    key_exclude_ids,
                ) = _resolve_key_specific_filters(request, k)
                if key_filter_expr or key_from_ids or key_exclude_ids:
                    has_key_specific_filters = True
                    break

            # Only use bulk computation if there are no key-specific filters
            if not has_key_specific_filters:
                # Use bulk computation for all keys with the same metric
                bulk_results = compute_metric_bulk(
                    keys=all_keys,
                    metric=common_metric,
                    project_id=project_obj.id,
                    context_id=context_id,
                    field_types=field_types,
                    filter_expr=request.filter_expr,
                    from_ids=request.from_ids,
                    exclude_ids=request.exclude_ids,
                    session=session,
                )

                # Post-process each value
                for k, value in bulk_results.items():
                    results[k] = value

                # Return single value or dictionary based on input type
                return results[all_keys[0]] if single_key else results

        # Fallback to per-key computation if metrics differ or key-specific filters exist
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
    context: Optional[str] = Query(
        None,
        description="Static context to filter logs by.",
        example="training",
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
    session=Depends(get_db_session),
) -> Dict[str, Any]:
    """
    Returns a dict with the different versions as keys and the values of the remaining
    items within a given project based on its key.
    The logs can be filtered using filter_expr, from_ids, and exclude_ids parameters
    before grouping.
    """
    # Instantiate DAOs with shared session
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    field_type_dao = FieldTypeDAO(session)

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
        context=context,
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


@router.patch(
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
    session=Depends(get_db_session),
):
    """
    Renames a field across all logs in a project. This includes:
    - Updating the field type record
    - Renaming the field in all logs (regular and history)

    The operation is atomic - either all renames succeed or none do.
    """
    # Instantiate DAOs with shared session
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    field_type_dao = FieldTypeDAO(session)
    log_dao = LogDAO(session, context_dao)

    try:
        # Check if this is the protected Unity/Tasks context
        if request.project == "Unity" and request.context == "Tasks":
            raise HTTPException(
                status_code=403,
                detail="Cannot modify fields in the built-in Tasks table - it is immutable",
            )

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
        context_id = context_dao.get_or_create(project_id=project_id, name=context_name)
        if not context_id:
            raise HTTPException(
                status_code=404,
                detail=f"Context '{context_name}' not found",
            )

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


@router.post(
    "/logs/join",
    responses={
        200: {
            "description": "Logs joined successfully",
            "content": {
                "application/json": {
                    "example": {
                        "info": "Joined logs created successfully!",
                    },
                },
            },
        },
        400: {
            "description": "Bad Request",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Invalid join parameters. Check your request and try again.",
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
def join_logs(
    request_fastapi: Request,
    request: JoinLogsRequest,
    session=Depends(get_db_session),
):
    """
    Joins two sets of logs based on specified criteria and creates new logs with the joined data.

    The join operation is similar to SQL joins, allowing inner, left, right, and outer joins
    between two sets of logs filtered by the criteria in pair_of_args.

    Args:
        pair_of_args: List of two dictionaries containing filtering criteria for logs to join.
                     Each dictionary can include context, filter_expr, from_ids, etc.
        join_expr: SQL expression for the join condition using aliases A and B
                  (e.g., 'A.user_id = B.user_id')
        mode: Type of join to perform ('inner', 'left', 'right', or 'outer')
        new_context: Name for the new context where joined logs will be stored
        columns: Optional list of column names to include in the joined result
        project: Name of the project containing the logs

    Returns:
        JSON response with info about the join operation
    """
    # Instantiate DAOs with shared session
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    field_type_dao = FieldTypeDAO(session)

    # Validate input parameters
    user_id = request_fastapi.state.user_id

    # Validate project
    try:
        project_obj = project_dao.get_by_user_and_name(
            user_id=user_id,
            name=request.project,
        )
        project_id = project_obj.id
    except (IndexError, AttributeError):
        raise HTTPException(
            status_code=404,
            detail=f"Project '{request.project}' not found.",
        )

    # Validate pair_of_args
    if not isinstance(request.pair_of_args, list) or len(request.pair_of_args) != 2:
        raise HTTPException(
            status_code=400,
            detail="pair_of_args must be a list containing exactly two dictionaries.",
        )

    # Validate join mode
    valid_modes = ["inner", "left", "right", "outer"]
    if request.mode not in valid_modes:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid join mode. Must be one of: {', '.join(valid_modes)}",
        )

    # Validate join expression
    if not request.join_expr or not isinstance(request.join_expr, str):
        raise HTTPException(
            status_code=400,
            detail="join_expr must be a non-empty string.",
        )

    # Validate new_context
    if not request.new_context or not isinstance(request.new_context, str):
        raise HTTPException(
            status_code=400,
            detail="new_context must be a non-empty string.",
        )

    # Create or get the new context
    try:
        context_id = context_dao.get_or_create(
            project_id=project_id,
            name=request.new_context,
            description=f"Joined logs context created via join operation ({request.mode} join)",
            is_versioned=False,
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create context '{request.new_context}': {str(e)}",
        )

    # Perform the join operation
    try:
        new_log_ids = _join_logs(
            project_name=request.project,
            project_id=project_id,
            pair_of_args=request.pair_of_args,
            join_expr=request.join_expr,
            mode=request.mode,
            context_id=context_id,
            columns=request.columns,
            copy=request.copy,
            request_fastapi=request_fastapi,
            project_dao=project_dao,
            field_type_dao=field_type_dao,
            context_dao=context_dao,
            session=session,
        )

        return {
            "info": f"Successfully joined logs with {request.mode} join and stored in context '{request.new_context}'",
        }

    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail=str(e),
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error performing join operation: {str(e)}",
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
                            "unique": "false",
                            "created_at": "2025-02-14T10:00:00Z",
                            "artifacts": "",
                            "description": "this field is a dummy field",
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
        description="Optional context name to filter fields types",
        example="training",
    ),
    session=Depends(get_db_session),
):
    """
    Returns a dictionary of fields names and their types for the specified project.
    If a context is provided, returns only fields associated with that context.

    Each field entry contains:
    - data_type: The data type of the field (int, str, etc)
    - field_type: Whether it's an entry, param, or derived_entry
    - mutable: Whether the field can be modified
    - unique: Whether the field enforces uniqueness
    - created_at: When the field was first created
    - artifacts: For derived entries, contains the equation
    - description: The description of the field
    """
    # Instantiate DAOs with shared session
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    field_type_dao = FieldTypeDAO(session)

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
        .join(LogEventDerivedLog, LogEventDerivedLog.derived_log_id == DerivedLog.id)
        .join(LogEvent, LogEvent.id == LogEventDerivedLog.log_event_id)
        .filter(LogEvent.project_id == project_obj.id)
        .distinct()
        .all()
    )
    for key, equation in derived_fields:
        derived_equations[key] = equation

    # Build response
    return {
        key: {
            "data_type": info[
                "field_type"
            ],  # Full type: "List[int]", "str", "Any", etc.
            "field_type": info["field_category"],
            "mutable": info["mutable"],
            "unique": info.get("unique", False),
            "enum_values": info["enum_values"],
            "restrict": info["restrict"],
            "created_at": info["created_at"],
            "artifacts": derived_equations.get(key, ""),
            "description": info.get("description", ""),
        }
        for key, info in types.items()
    }


@router.post(
    "/logs/fields",
    responses={
        200: {
            "description": "Fields created successfully",
            "content": {
                "application/json": {
                    "example": {
                        "info": "Fields created successfully.",
                    },
                },
            },
        },
        404: {
            "description": "Project or context not found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Project 'example_project' not found.",
                    },
                },
            },
        },
    },
)
def create_fields(
    request_fastapi: Request,
    request: CreateFieldsRequest,
    session=Depends(get_db_session),
):
    """
    Creates one or more fields in a project. Fields are field definitions that can be used
    in logs. This endpoint allows pre-defining fields before adding any log data.

    Each field can have an optional description. If a field already exists, its description
    will be updated.
    """
    # Instantiate DAOs with shared session
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    field_type_dao = FieldTypeDAO(session)

    # Validate project
    try:
        user_id = request_fastapi.state.user_id
        project = project_dao.get_by_user_and_name(
            user_id=user_id,
            name=request.project,
        )
        project_id = project.id
    except (IndexError, AttributeError):
        raise HTTPException(
            status_code=404,
            detail=f"Project '{request.project}' not found.",
        )

    # Get or create context
    context_name = request.context if request.context else ""
    context_id = context_dao.get_or_create(
        project_id=project_id,
        name=context_name,
        description=None,
        is_versioned=False,
    )

    # Create fields
    try:
        field_type_dao.create_fields(
            project_id=project_id,
            context_id=context_id,
            fields=request.fields,
        )
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Failed to create fields: {str(e)}",
        )

    # Backfill existing logs with None values if requested
    backfilled_count = 0
    if request.backfill_logs:
        try:
            # Get all log events in this context
            le_rows = (
                session.query(LogEvent.id)
                .join(LogEventContext, LogEventContext.log_event_id == LogEvent.id)
                .filter(
                    LogEvent.project_id == project_id,
                    LogEventContext.context_id == context_id,
                )
                .all()
            )

            log_event_ids = [row[0] for row in le_rows]

            if log_event_ids and request.fields:
                field_names = list(request.fields.keys())

                # Query existing base (log_event_id, key) pairs in one shot
                existing_base_pairs = (
                    session.query(LogEventLog.log_event_id, Log.key)
                    .join(Log, Log.id == LogEventLog.log_id)
                    .filter(
                        LogEventLog.log_event_id.in_(log_event_ids),
                        Log.key.in_(field_names),
                    )
                    .all()
                )

                # Query existing derived (log_event_id, key) pairs in one shot
                existing_derived_pairs = (
                    session.query(LogEventDerivedLog.log_event_id, DerivedLog.key)
                    .join(
                        DerivedLog,
                        DerivedLog.id == LogEventDerivedLog.derived_log_id,
                    )
                    .filter(
                        LogEventDerivedLog.log_event_id.in_(log_event_ids),
                        DerivedLog.key.in_(field_names),
                    )
                    .all()
                )

                existing_pairs = set(existing_base_pairs) | set(existing_derived_pairs)

                # Prepare entries to create for missing pairs only
                entries_to_create = []
                for le_id in log_event_ids:
                    for fname in field_names:
                        if (le_id, fname) in existing_pairs:
                            continue
                        entries_to_create.append(
                            {
                                "project_id": project_id,
                                "log_event_id": le_id,
                                "key": fname,
                                "value": None,
                                "context_id": context_id,
                            },
                        )

                backfilled_count = len(entries_to_create)

                if entries_to_create:
                    # Create LogDAO instance for bulk_create
                    log_dao = LogDAO(session, context_dao)
                    log_dao.bulk_create(entries_to_create)
                    session.commit()
        except Exception as e:
            session.rollback()
            raise HTTPException(
                status_code=500,
                detail=f"Failed to backfill logs: {str(e)}",
            )

    return {
        "info": f"Fields created successfully. {'Backfilled ' + str(backfilled_count) + ' log entries with None values.' if request.backfill_logs and backfilled_count > 0 else ''}",
        "backfilled_count": backfilled_count if request.backfill_logs else 0,
    }


@router.delete(
    "/logs/fields",
    responses={
        200: {
            "description": "Fields deleted successfully",
            "content": {
                "application/json": {
                    "example": {
                        "info": "Fields deleted successfully.",
                        "deleted_fields": ["score", "response"],
                    },
                },
            },
        },
        404: {
            "description": "Project or context not found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Project 'example_project' not found.",
                    },
                },
            },
        },
    },
)
def delete_fields(
    request_fastapi: Request,
    request: DeleteFieldsRequest,
    session=Depends(get_db_session),
):
    """
    Deletes one or more fields from a project. This will:
    1. Delete all Log and DerivedLog entries with the specified field names (not the entire LogEvent)
    2. Delete the field type records for those fields

    This operation cannot be undone, so use with caution.
    """
    # Instantiate DAOs with shared session
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    field_type_dao = FieldTypeDAO(session)
    log_dao = LogDAO(session, context_dao)

    # Check if this is the protected Unity/Tasks context
    if request.project == "Unity" and request.context == "Tasks":
        raise HTTPException(
            status_code=403,
            detail="Cannot modify fields in the built-in Tasks table - it is immutable",
        )

    # Validate project
    try:
        user_id = request_fastapi.state.user_id
        project = project_dao.get_by_user_and_name(
            user_id=user_id,
            name=request.project,
        )
        project_id = project.id
    except (IndexError, AttributeError):
        raise HTTPException(
            status_code=404,
            detail=f"Project '{request.project}' not found.",
        )

    # Get context
    context_name = request.context if request.context else ""
    context = context_dao.filter(project_id=project_id, name=context_name)
    if not context:
        raise HTTPException(
            status_code=404,
            detail=f"Context '{context_name}' not found.",
        )
    context_id = context[0][0].id

    deleted_fields = []
    total_deleted_logs = 0
    total_deleted_derived_logs = 0

    for field_name in request.fields:
        try:
            # Get all log event IDs that have this field in either base logs or derived logs
            base_log_events = (
                session.query(LogEventLog.log_event_id)
                .join(Log, Log.id == LogEventLog.log_id)
                .join(LogEvent, LogEvent.id == LogEventLog.log_event_id)
                .filter(
                    LogEvent.project_id == project_id,
                    Log.key == field_name,
                )
                .distinct()
            )

            derived_log_events = (
                session.query(LogEventDerivedLog.log_event_id)
                .join(DerivedLog, DerivedLog.id == LogEventDerivedLog.derived_log_id)
                .join(LogEvent, LogEvent.id == LogEventDerivedLog.log_event_id)
                .filter(
                    LogEvent.project_id == project_id,
                    DerivedLog.key == field_name,
                )
                .distinct()
            )

            # Combine both queries with UNION to get all affected log event IDs
            all_event_ids = base_log_events.union(derived_log_events).all()
            event_ids = [event_id[0] for event_id in all_event_ids]

            if event_ids:
                # First, find the Log IDs to delete
                logs_to_delete_ids = (
                    session.query(Log.id)
                    .join(
                        LogEventLog,
                        LogEventLog.log_id == Log.id,
                    )
                    .filter(
                        LogEventLog.log_event_id.in_(event_ids),
                        Log.key == field_name,
                    )
                    .all()
                )
                log_ids = [log_id[0] for log_id in logs_to_delete_ids]

                if log_ids:
                    # Query for Log entries to delete (for GCS cleanup)
                    logs_to_delete_query = session.query(Log).filter(
                        Log.id.in_(log_ids),
                    )

                    # Delete GCS media files before deleting database records
                    log_dao._bulk_delete_gcs_media(logs_to_delete_query)

                    # Delete the Log entries (not the LogEvents!)
                    deleted_logs_count = logs_to_delete_query.delete(
                        synchronize_session=False,
                    )
                    total_deleted_logs += deleted_logs_count
                else:
                    deleted_logs_count = 0

                # Delete the DerivedLog entries
                # First, find the DerivedLog IDs to delete
                derived_logs_to_delete_ids = (
                    session.query(DerivedLog.id)
                    .join(
                        LogEventDerivedLog,
                        LogEventDerivedLog.derived_log_id == DerivedLog.id,
                    )
                    .filter(
                        LogEventDerivedLog.log_event_id.in_(event_ids),
                        DerivedLog.key == field_name,
                    )
                    .all()
                )
                derived_log_ids = [d[0] for d in derived_logs_to_delete_ids]

                # Then delete them
                deleted_derived_logs_count = 0
                if derived_log_ids:
                    deleted_derived_logs_count = (
                        session.query(DerivedLog)
                        .filter(DerivedLog.id.in_(derived_log_ids))
                        .delete(synchronize_session=False)
                    )
                total_deleted_derived_logs += deleted_derived_logs_count

            # Delete field type record
            field_type_dao.delete_field_type(
                project_id=project_id,
                field_name=field_name,
                context_id=context_id,
            )

            deleted_fields.append(field_name)
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"Error deleting field {field_name}: {str(e)}",
            )

    if not deleted_fields:
        return {
            "info": "No fields were deleted. They may not exist or you don't have permission to delete them.",
            "deleted_fields": [],
        }

    return {
        "info": f"Fields deleted successfully. Removed {total_deleted_logs} logs and {total_deleted_derived_logs} derived logs.",
        "deleted_fields": deleted_fields,
    }


######################
# Admin endpoints
######################


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
    _=Depends(auth_admin_key),
):
    """
    Admin endpoint to process active derived logs and create new derived logs
    for new log events that match the filter criteria.
    This endpoint  is designed to be calledby internal processes (e.g., Cloud Scheduler) or administrators.
    """
    # Instantiate DAO with shared session
    field_type_dao = FieldTypeDAO(session)

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
                session.query(LogEventDerivedLog.log_event_id)
                .join(DerivedLog, DerivedLog.id == LogEventDerivedLog.derived_log_id)
                .filter(
                    DerivedLog.key == template.key,
                    LogEventDerivedLog.log_event_id.in_(select(all_log_events.c.id)),
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
                    derived_log_associations = (
                        []
                    )  # Track (log_event_id, derived_log_index)

                    for log_event_id, (_, value) in zip(
                        matching_log_event_ids,
                        computed_values,
                    ):
                        val = json.loads(json.dumps(value, cls=CustomEncoder))
                        inferred_type = LogDAO.infer_type("", val)

                        # Create DerivedLog without log_event_id
                        new_derived_logs.append(
                            DerivedLog(
                                key=template.key,
                                equation=template.equation,
                                referenced_logs=template.referenced_logs,
                                value=val,
                                inferred_type=inferred_type,
                                created_at=datetime.now(timezone.utc),
                                updated_at=datetime.now(timezone.utc),
                            ),
                        )
                        # Track the association
                        derived_log_associations.append(
                            (log_event_id, len(new_derived_logs) - 1),
                        )

                    # Bulk insert the new derived logs
                    if new_derived_logs:
                        session.bulk_save_objects(
                            new_derived_logs,
                            return_defaults=True,
                        )
                        session.flush()  # Get IDs for the new derived logs

                        # Create LogEventDerivedLog associations
                        for log_event_id, derived_log_index in derived_log_associations:
                            if derived_log_index < len(new_derived_logs):
                                association = LogEventDerivedLog(
                                    log_event_id=log_event_id,
                                    derived_log_id=new_derived_logs[
                                        derived_log_index
                                    ].id,
                                )
                                session.add(association)

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
    _=Depends(auth_admin_key),
):
    """
    Admin endpoint to manually pull and process traffic log messages from PubSub.
    This endpoint is designed to be called by internal processes (e.g., Cloud Scheduler) or administrators.
    """
    # Instantiate DAOs with shared session
    organization_dao = OrganizationDAO(session)
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    field_type_dao = FieldTypeDAO(session)
    log_event_dao = LogEventDAO(session)
    log_dao = LogDAO(session, context_dao)
    try:
        from google.cloud import pubsub_v1

        from orchestra.settings import settings

        # 1. Fetch the 'Production Traffic' project
        ORGANIZATION_NAME = settings.orchestra_organization_name
        PROJ_NAME = settings.orchestra_prod_traffic_name
        admin_org = organization_dao.filter(name=ORGANIZATION_NAME)[0][0]
        project_id = project_dao.filter(organization_id=admin_org.id, name=PROJ_NAME)[
            0
        ][0].id
        context_id = context_dao.get_or_create(
            project_id,
            name="",
            description=None,
            is_versioned=False,
        )
        context_obj = session.get(Context, context_id)
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
            timeout=40,
        )

        entries: List[Dict[str, Any]] = []
        ack_ids: List[str] = []

        for received_message in response.received_messages:
            ack_ids.append(received_message.ack_id)

            try:
                data = json.loads(received_message.message.data.decode("utf-8"))
            except json.JSONDecodeError:
                # Malformed payload – acknowledge and skip so it doesn't poison the queue
                continue

            for key in ("project_id", "context_id", "project_name"):
                data.pop(key, None)

            entries.append(data)

        if not entries:
            return {"message": "No new traffic-log messages", "status": "success"}

        # batch ingestion
        create_logs_internal(
            project_id=project_id,
            context_id=context_id,
            request=CreateLogConfig(
                entries=entries,
                project=PROJ_NAME,
                context=None,
            ),
            project_dao=project_dao,
            field_type_dao=field_type_dao,
            log_event_dao=log_event_dao,
            log_dao=log_dao,
            context_dao=context_dao,
            context_obj=context_obj,
        )

        processed_count = len(entries)
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

        error_message = traceback.format_exc()
        return JSONResponse(
            status_code=500,
            content={"detail": f"Error processing traffic logs: {error_message}"},
        )
