"""

Includes endpoints related to Log API.
"""

import json
import logging
import re
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
from sqlalchemy import and_, exists, or_, select, text
from sqlalchemy.exc import DataError, SQLAlchemyError
from sqlalchemy.sql.selectable import Subquery

from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dao.field_type_dao import FieldTypeDAO
from orchestra.db.dao.log_event_dao import (
    ImmutableFieldError,
    LogEventDAO,
    OverwriteError,
    _extract_field_names_from_equation,
)
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.project_dao import ProjectDAO
from orchestra.db.dao.resource_access_dao import ResourceAccessDAO
from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import (
    ActiveDerivedLog,
    Context,
    Embedding,
    LogEvent,
    LogEventContext,
    Project,
)
from orchestra.services.task_machine_state_service import (
    TASK_MACHINE_PROJECT_NAME,
    get_task_ids_for_log_ids,
    is_internal_task_machine_context_name,
    is_protected_task_surface_context_name,
    is_task_surface_context_name,
    sync_task_activations_for_task_ids,
)
from orchestra.web.api.dependencies import auth_admin_key
from orchestra.web.api.log.schema import (
    AtomicFieldUpdateRequest,
    AtomicFieldUpdateResponse,
    CreateDerivedEntriesConfig,
    CreateFieldsRequest,
    CreateLogConfig,
    DeleteFieldsRequest,
    DeleteLogEntryRequest,
    GetLogsMetricRequest,
    JoinLogsRequest,
    JoinQueryRequest,
    QueryLogsPostBody,
    RenameFieldRequest,
    UpdateDerivedEntriesConfig,
    UpdateFieldRequest,
    UpdateLogRequest,
)
from orchestra.web.api.utils.helpers import CustomEncoder
from orchestra.web.api.utils.http_responses import not_found

from .python2SQL import (
    _compute_expression,
    _extract_placeholders,
    _substitute_placeholders,
    build_sql_query,
    str_filter_exp_to_dict,
)
from .task_machine_admin import router as task_machine_admin_router
from .utils import (
    _build_grouped_data,
    _compute_metric_for_key_grouped,
    _fetch_logs_for_event_ids,
    _flatten_fields,
    _format_flat_logs,
    _format_logs,
    _get_all_filtered_log_event_ids,
    _get_distinct_group_values,
    _get_log_event_ids_for_group_value,
    _get_logs_query,
    _join_logs,
    _join_query_internal,
    _resolve_key_specific_filters,
    apply_group_threshold,
    compute_metric_bulk,
    compute_metric_for_key,
    create_logs_internal,
)

router = APIRouter()

# Admin router for protected endpoints
admin_router = APIRouter()
admin_router.include_router(task_machine_admin_router)


def _require_mutable_task_machine_context(
    *,
    project_name: str,
    context_name: str | None,
) -> None:
    """Reject writes against system-managed task-machine contexts."""

    if (
        project_name == TASK_MACHINE_PROJECT_NAME
        and is_internal_task_machine_context_name(context_name)
    ) or (
        project_name == "Unity" and is_protected_task_surface_context_name(context_name)
    ):
        raise HTTPException(
            status_code=403,
            detail="Cannot modify protected task machine contexts - they are system-managed",
        )


def _check_project_write_permission(
    session,
    user_id: str,
    organization_id: Optional[int],
    project_id: int,
) -> None:
    """Enforce project:write for org-context requests. Personal context is always allowed."""
    if organization_id is None:
        return
    ra_dao = ResourceAccessDAO(session)
    if not ra_dao.check_user_permission(
        user_id,
        "project",
        project_id,
        "project:write",
    ):
        raise HTTPException(
            status_code=403,
            detail="You do not have permission to write to this project",
        )


def _sanitize_sql_error(error: Exception) -> str:
    """
    Extract a clean error message from SQLAlchemy exceptions, removing SQL traces.

    Args:
        error: The SQLAlchemy exception

    Returns:
        A clean error message without SQL statements
    """
    error_msg = str(error.orig) if hasattr(error, "orig") and error.orig else str(error)

    # Remove SQL statement and parameters from error if present
    if "[SQL:" in error_msg:
        error_msg = error_msg.split("[SQL:")[0].strip()

    # Extract just the PostgreSQL error message (remove psycopg2 wrapper)
    if "psycopg2.errors." in error_msg:
        # Format: "psycopg2.errors.InvalidDatetimeFormat: invalid input syntax..."
        parts = error_msg.split(":", 1)
        if len(parts) > 1:
            error_msg = parts[1].strip()

    # Remove any remaining traceback-like content
    if "Traceback" in error_msg:
        error_msg = error_msg.split("Traceback")[0].strip()

    return error_msg


# Import sibling context cleanup from shared module
from orchestra.db.dao.sibling_context_cleanup import (
    get_assistants_sibling_context_info as _get_assistants_sibling_context_info,
)


def _recompute_derived_for_logs(
    *,
    session,
    project_id: int,
    context_id: int,
    log_ids: list,
    entry_keys: set,
    derived_log_dao,
    field_type_dao,
):
    """Recompute derived columns for the given log IDs.

    Finds ActiveDerivedLog templates whose ``referenced_keys`` overlap with
    *entry_keys* and calls ``recompute_derived_logs`` for each template,
    scoped to *log_ids*.  Used by both the create and update handlers.
    """
    if not entry_keys or not log_ids:
        return

    try:
        from sqlalchemy import cast
        from sqlalchemy.dialects.postgresql import JSONB

        key_conditions = [
            ActiveDerivedLog.referenced_keys.op("@>")(cast([key], JSONB))
            for key in entry_keys
        ]

        dependent_templates = (
            session.query(ActiveDerivedLog)
            .filter(
                ActiveDerivedLog.project_id == project_id,
                ActiveDerivedLog.context_id == context_id,
                ActiveDerivedLog.is_active == True,
                or_(*key_conditions),
            )
            .all()
        )

        processed_template_ids: set = set()

        for template in dependent_templates:
            if template.id in processed_template_ids:
                continue
            processed_template_ids.add(template.id)

            try:
                derived_log_dao.recompute_derived_logs(
                    template=template,
                    log_ids=log_ids,
                    json_encoder=CustomEncoder,
                    field_type_dao=field_type_dao,
                )
            except Exception as template_error:
                logging.warning(
                    f"Failed to recompute derived logs for template "
                    f"'{template.key}': {template_error}",
                )
    except Exception as ripple_error:
        logging.warning(
            f"Error in derived recomputation for project {project_id}: "
            f"{ripple_error}",
        )


def _sync_task_activations_if_needed(
    *,
    session,
    project_name: str | None,
    project_id: int,
    task_ids: Set[int],
    tasks_context_name: str | None,
) -> None:
    """Project task machine state when the user-authored Tasks table changed."""

    if (
        project_name != TASK_MACHINE_PROJECT_NAME
        or not task_ids
        or not tasks_context_name
        or not is_task_surface_context_name(tasks_context_name)
    ):
        return
    sync_task_activations_for_task_ids(
        session=session,
        project_id=project_id,
        task_ids=task_ids,
        tasks_context_name=tasks_context_name,
    )


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
            "mutable": false,  # Makes the field immutable (default is true)
            "unique": true     # Makes the field unique
        }
    }
    ```

    By default, all fields are mutable. Set `mutable: false` to make a field
    immutable after creation. Only mutable fields can be modified through
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
    log_dao = LogEventDAO(session, context_dao)

    # check if the project exists
    try:
        user_id = request_fastapi.state.user_id
        organization_id = getattr(request_fastapi.state, "organization_id", None)
        project = project_dao.get_by_user_and_name(
            user_id=user_id,
            name=request.project_name,
            organization_id=organization_id,
        )
        project_id = project.id
    except (IndexError, AttributeError):
        raise not_found("Project")

    _check_project_write_permission(session, user_id, organization_id, project_id)

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
            context_dao=context_dao,
        )

        # Final sanity: if nothing succeeded and there are failures, surface 400
        if not result.get("log_event_ids") and result.get("failed"):
            first_error = result["failed"][0].get("error", "Log creation failed")
            raise HTTPException(status_code=400, detail=first_error)

        # Opt-in derived column recomputation for the newly created logs.
        if request.recompute_derived and result.get("log_event_ids"):
            entries_list = (
                request.entries
                if isinstance(request.entries, list)
                else [request.entries]
            )
            entry_keys: set = set()
            for entry in entries_list:
                if isinstance(entry, dict):
                    entry_keys.update(entry.keys())

            derived_log_dao = LogEventDAO(session, context_dao)
            _recompute_derived_for_logs(
                session=session,
                project_id=project_id,
                context_id=context_id,
                log_ids=result["log_event_ids"],
                entry_keys=entry_keys,
                derived_log_dao=derived_log_dao,
                field_type_dao=field_type_dao,
            )

        if (
            project.name == TASK_MACHINE_PROJECT_NAME
            and context_obj
            and is_task_surface_context_name(context_obj.name)
            and result.get("log_event_ids")
        ):
            task_ids = get_task_ids_for_log_ids(
                session=session,
                project_id=project_id,
                context_name=context_obj.name,
                log_event_ids=result["log_event_ids"],
            )
            _sync_task_activations_if_needed(
                session=session,
                project_name=project.name,
                project_id=project_id,
                task_ids=task_ids,
                tasks_context_name=context_obj.name,
            )

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

    # Helper to extract log IDs from query results
    def _extract_log_ids(query_dict: dict) -> Set[int]:
        """Run query and extract log IDs."""
        rows, _count = _get_logs_query(
            request_fastapi=request_fastapi,
            project_name=project_name,
            context=query_dict.get("context"),
            filter_expr=query_dict.get("filter_expr"),
            sorting=query_dict.get("sorting"),
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
        return {r[0] for r in rows}  # r[0] is log_event_id

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
            le_ids = _extract_log_ids(base_dict)
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

                le_ids = _extract_log_ids(query_dict)

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
      Eagerly computes each derived value and stores it in LogEvent.data.

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
    log_dao = LogEventDAO(session, context_dao)

    user_id = request_fastapi.state.user_id
    organization_id = getattr(request_fastapi.state, "organization_id", None)

    # 1) Validate the project
    try:
        project_obj = project_dao.get_by_user_and_name(
            name=body.project_name,
            user_id=user_id,
            organization_id=organization_id,
        )
        project_id = project_obj.id
    except (IndexError, AttributeError):
        raise HTTPException(
            status_code=404,
            detail=f"Project '{body.project_name}' not found.",
        )
    _check_project_write_permission(session, user_id, organization_id, project_id)

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
        project_name=body.project_name,
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
                log_dao.bulk_update(
                    updates,
                    overwrite=True,
                    field_types=field_types,
                )

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
        # Materialize derived values into LogEvent.data
        try:
            # Check if this is a filter-based derived log and extract filter_expression
            filter_expression = None
            if isinstance(body.referenced_logs, dict):
                for key, value in body.referenced_logs.items():
                    if isinstance(value, dict) and "filter_expr" in value:
                        filter_expression = body.referenced_logs
                        break

            # Build filter_dict and compute values
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

            # Pass target key for embedding operations (SYNC workflow)
            # This ensures embeddings are created with the correct key (body.key)
            # instead of the source field name from the equation
            filter_dict["embed_target_key"] = body.key

            resolved_ids_dict = {}
            for key, ids in resolved_ids.items():
                resolved_ids_dict.setdefault(alias_to_key_map[key], []).extend(ids)

            # Flatten all referenced log_event_ids
            filtered_log_ids = list(
                {int(i) for ids in resolved_ids_dict.values() for i in ids},
            )

            # Get filtered log events
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
                project_id=project_obj.id,
                context_id=context_id,
            )

            # Track which IDs were computed vs requested for failure reporting
            requested_ids = set(filtered_log_ids)
            computed_ids = {log_id for log_id, _ in computed_values}
            not_found_ids = sorted(requested_ids - computed_ids)

            if not computed_values:
                response = {"info": "No values computed. Nothing to create."}
                if not_found_ids:
                    response["not_found"] = not_found_ids
                return response

            # Create index mappings for each alias
            alias_to_id_list = {}
            alias_to_index_map = {}
            for alias, id_list in resolved_ids.items():
                alias_to_id_list[alias] = id_list
                alias_to_index_map[alias] = {
                    log_id: idx for idx, log_id in enumerate(id_list)
                }

            # Prepare bulk update data
            updates = []
            embedding_objects = []  # Collect embeddings for bulk insertion
            non_null_val = None
            placeholders = _extract_placeholders(body.equation)
            referenced_logs = {
                ph.split(":")[1]: v
                for ph in placeholders
                for k, v in body.referenced_logs.items()
                if k in ph
            }

            for computed_log_id, value in computed_values:
                source_index = None
                for alias, index_map in alias_to_index_map.items():
                    if computed_log_id in index_map:
                        source_index = index_map[computed_log_id]
                        break

                if source_index is not None:
                    for alias, id_list in alias_to_id_list.items():
                        if source_index < len(id_list):
                            log_event_id = id_list[source_index]

                            # Check for vector FIRST to skip expensive JSON serialization
                            if isinstance(value, np.ndarray):
                                # Vectors are stored in Embedding table, not in LogEvent.data
                                # (JSONB can't hold numpy arrays; we store NULL as a marker)
                                val = None
                                non_null_val = value.tolist()

                                # NOTE: This special handling for image embeddings is confusing.
                                #
                                # For some reason, embed() and embed_image() have inconsistent
                                # storage flows:
                                #
                                # - embed(): Storage happens inside _handle_embed_jsonb via
                                #   _ensure_vectors_exist() - embeddings are stored BEFORE
                                #   results are returned here.
                                #
                                # - embed_image(): Storage does NOT happen in the handler
                                #   (_handle_embed_image_jsonb). It only computes embeddings
                                #   on-the-fly and returns them, so we MUST store them here.
                                #
                                # TODO: Consider unifying this. Why should image embeddings have
                                # a completely different storage path than text embeddings? This
                                # bespoke logic is error-prone and adds maintenance burden. Ideally
                                # both should follow the same pattern (either both store in their
                                # handlers, or both store here).
                                is_image_embedding = "embed_image(" in body.equation
                                if is_image_embedding:
                                    from orchestra.web.api.log.python2SQL.helpers import (
                                        DEFAULT_IMAGE_EMBEDDING_MODEL,
                                    )

                                    embedding_obj = Embedding(
                                        ref_id=log_event_id,
                                        key=body.key,
                                        model=DEFAULT_IMAGE_EMBEDDING_MODEL,
                                        vector=value,
                                    )
                                    embedding_objects.append(embedding_obj)
                            else:
                                # Standard path for non-vector data
                                val = json.loads(
                                    json.dumps(value, cls=CustomEncoder),
                                )
                                if val is not None:
                                    non_null_val = val

                            # Add to bulk update list
                            updates.append(
                                {
                                    "log_event_id": log_event_id,
                                    "key": body.key,
                                    "value": val,
                                    "context_id": context_id,
                                    "overwrite": True,
                                },
                            )

            # Bulk insert embeddings in a single operation
            if embedding_objects:
                session.bulk_save_objects(embedding_objects)

            # Execute bulk update
            if updates:
                log_dao.bulk_update(
                    updates,
                    field_types=field_types,
                    overwrite=True,
                )

            # Create/update ActiveDerivedLog template only when derived=True
            inferred_type = LogEventDAO.infer_type("", non_null_val)

            if body.derived:
                existing_template = (
                    session.query(ActiveDerivedLog)
                    .filter(
                        ActiveDerivedLog.project_id == project_obj.id,
                        ActiveDerivedLog.key == body.key,
                        ActiveDerivedLog.context_id == context_id,
                    )
                    .first()
                )

                referenced_keys = _extract_field_names_from_equation(body.equation)

                if not existing_template:
                    template = ActiveDerivedLog(
                        project_id=project_obj.id,
                        context_id=context_id,
                        key=body.key,
                        equation=body.equation,
                        referenced_logs=referenced_logs,
                        filter_expression=filter_expression,
                        inferred_type=inferred_type,
                        referenced_keys=referenced_keys,
                        is_active=True,
                        created_at=datetime.now(timezone.utc),
                        updated_at=datetime.now(timezone.utc),
                    )
                    session.add(template)
                else:
                    existing_template.equation = body.equation
                    existing_template.referenced_logs = referenced_logs
                    existing_template.filter_expression = filter_expression
                    existing_template.inferred_type = inferred_type
                    existing_template.referenced_keys = referenced_keys
                    existing_template.is_active = True
                    existing_template.updated_at = datetime.now(timezone.utc)

            session.commit()

            # Create field type with appropriate category
            # Check if this is an embedding field based on the equation
            is_embedding = "embed(" in body.equation or "embed_image(" in body.equation
            field_category = "derived_entry" if body.derived else "entry"
            field_type_dao.create_field_type_if_absent(
                project_id=project_obj.id,
                field_name=body.key,
                value=non_null_val,
                field_category=field_category,
                context_id=context_id,
                field_type="vector" if is_embedding else None,
                infer_type=not is_embedding,
                # Derived entries are always immutable (computed values shouldn't be updated)
                mutable=field_category != "derived_entry",
            )
            session.commit()

            response = {
                "info": f"Created {len(updates)} derived logs with key='{body.key}'.",
            }
            if not_found_ids:
                response["not_found"] = not_found_ids
            return response

        except Exception as e:
            session.rollback()
            raise HTTPException(
                status_code=500,
                detail=f"Failed to create derived logs with key='{body.key}'. Error: {e}",
            )


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
    derived_log_dao = LogEventDAO(session, context_dao)

    user_id = request_fastapi.state.user_id
    organization_id = getattr(request_fastapi.state, "organization_id", None)

    # 1) Validate the project
    try:
        project_obj = project_dao.get_by_user_and_name(
            name=body.project_name,
            user_id=user_id,
            organization_id=organization_id,
        )
        project_id = project_obj.id
    except (IndexError, AttributeError):
        raise HTTPException(
            status_code=404,
            detail=f"Project '{body.project_name}' not found.",
        )
    _check_project_write_permission(session, user_id, organization_id, project_id)

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

    # Update derived log templates and recompute
    try:
        # Query ActiveDerivedLog templates matching the update criteria
        template_query = session.query(ActiveDerivedLog).filter(
            ActiveDerivedLog.project_id == project_id,
            ActiveDerivedLog.context_id == context_id,
        )

        # Filter by key if provided
        if updated_key:
            template_query = template_query.filter(
                ActiveDerivedLog.key == updated_key,
            )

        templates = template_query.all()

        if not templates:
            return {
                "info": "No templates found matching criteria. Nothing to update.",
            }

        updated_count = 0
        removed_count = 0
        for template in templates:
            # Track old referenced logs before updating (for removal logic)
            old_referenced_log_ids = set()
            if template.referenced_logs:
                for ref_ids in template.referenced_logs.values():
                    if isinstance(ref_ids, list):
                        old_referenced_log_ids.update(ref_ids)
                    elif isinstance(ref_ids, int):
                        old_referenced_log_ids.add(ref_ids)

            # Update template properties if provided
            if updated_equation:
                template.equation = updated_equation
                # Update referenced_keys when equation changes
                template.referenced_keys = _extract_field_names_from_equation(
                    updated_equation,
                )

            # Track new referenced log IDs
            new_referenced_log_ids = set()
            if new_refs:
                # Parse new referenced_logs structure
                placeholders = _extract_placeholders(
                    updated_equation or template.equation,
                )
                referenced_logs = {
                    ph.split(":")[1]: v
                    for ph in placeholders
                    for k, v in new_refs.items()
                    if k in ph
                }
                template.referenced_logs = referenced_logs

                # Collect new referenced log IDs
                for ref_ids in new_refs.values():
                    if isinstance(ref_ids, list):
                        new_referenced_log_ids.update(ref_ids)
                    elif isinstance(ref_ids, int):
                        new_referenced_log_ids.add(ref_ids)

                # Update filter_expression if new_refs contains filter_expr
                if isinstance(new_refs, dict):
                    for ref_key, ref_val in new_refs.items():
                        if isinstance(ref_val, dict) and "filter_expr" in ref_val:
                            template.filter_expression = new_refs
                            break

            # Remove derived field from logs that are no longer referenced
            logs_to_remove_from = old_referenced_log_ids - new_referenced_log_ids
            if logs_to_remove_from and template.key:
                # Use JSONB - operator to remove the key from data
                # data = data - 'key_name'
                from sqlalchemy import update as sql_update

                remove_stmt = (
                    sql_update(LogEvent)
                    .where(LogEvent.id.in_(logs_to_remove_from))
                    .values(
                        data=LogEvent.data.op("-")(template.key),
                        updated_at=datetime.now(timezone.utc),
                    )
                )
                result = session.execute(remove_stmt)
                removed_count += result.rowcount

            template.updated_at = datetime.now(timezone.utc)
            updated_count += 1

        session.commit()

        # Recompute if requested (default behavior)
        # Only recompute for logs that are in the template's referenced_logs
        recomputed_count = 0
        for template in templates:
            # Get log IDs from the template's referenced_logs
            log_ids = []
            if template.referenced_logs:
                for ref_ids in template.referenced_logs.values():
                    if isinstance(ref_ids, list):
                        log_ids.extend(ref_ids)
                    elif isinstance(ref_ids, int):
                        log_ids.append(ref_ids)
                log_ids = list(set(log_ids))  # Remove duplicates

            if log_ids:
                try:
                    count = derived_log_dao.recompute_derived_logs(
                        template=template,
                        log_ids=log_ids,
                        json_encoder=CustomEncoder,
                        field_type_dao=field_type_dao,
                    )
                    recomputed_count += count
                except Exception as recompute_error:
                    logging.warning(
                        f"Failed to recompute JSONB derived logs for template "
                        f"'{template.key}': {recompute_error}",
                    )

        return {
            "info": f"Updated {updated_count} templates, removed {removed_count} obsolete values, and recomputed {recomputed_count} derived values.",
        }

    except Exception as e:
        session.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"Failed to update derived logs. Error: {e}",
        )


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
    log_dao = LogEventDAO(session, context_dao)
    derived_log_dao = LogEventDAO(session, context_dao)

    return _update_logs(
        request_fastapi=request_fastapi,
        body=body,
        session=session,
        organization_member_dao=organization_member_dao,
        context_dao=context_dao,
        project_dao=project_dao,
        field_type_dao=field_type_dao,
        log_event_dao=log_event_dao,
        log_dao=log_dao,
        derived_log_dao=derived_log_dao,
    )


@router.patch(
    "/logs/{log_id}/fields/{field_name}/atomic",
    response_model=AtomicFieldUpdateResponse,
    responses={
        200: {
            "description": "Atomic operation applied successfully",
            "content": {
                "application/json": {
                    "example": {"new_value": 42.0},
                },
            },
        },
        400: {
            "description": "Invalid operation format",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Invalid operation format. Use +N, -N, *N, /N where N is a number.",
                    },
                },
            },
        },
        404: {
            "description": "Log not found",
            "content": {
                "application/json": {
                    "example": {"detail": "Log not found."},
                },
            },
        },
    },
)
def atomic_field_update(
    request_fastapi: Request,
    log_id: int = Path(..., description="The ID of the log to update"),
    field_name: str = Path(
        ...,
        description="The name of the field to update atomically",
    ),
    body: AtomicFieldUpdateRequest = Body(...),
    session=Depends(get_db_session),
):
    """
    Apply an atomic operation to a numeric field in a log entry.

    This endpoint performs race-safe atomic updates directly in PostgreSQL,
    ensuring correct results even under high concurrent load.

    Supported operations:
    - `+N`: Add N to the current value
    - `-N`: Subtract N from the current value
    - `*N`: Multiply the current value by N
    - `/N`: Divide the current value by N

    If the field doesn't exist or is NULL, it is treated as 0 before the operation.
    """
    return _atomic_field_update_impl(
        request_fastapi=request_fastapi,
        log_id=log_id,
        field_name=field_name,
        body=body,
        session=session,
    )


@router.post(
    "/logs/atomic",
    response_model=AtomicFieldUpdateResponse,
    responses={
        200: {
            "description": "Atomic upsert applied successfully",
            "content": {
                "application/json": {
                    "example": {
                        "log_id": 789,
                        "new_value": 83.50,
                        "created": False,
                        "mirrored_contexts": ["All/Spending/Monthly"],
                    },
                },
            },
        },
        400: {
            "description": "Invalid operation format or missing required fields",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Invalid operation format. Use +N, -N, *N, /N where N is a number.",
                    },
                },
            },
        },
        404: {
            "description": "Project not found",
            "content": {
                "application/json": {
                    "example": {"detail": "Project not found."},
                },
            },
        },
    },
)
def atomic_field_upsert(
    request_fastapi: Request,
    body: AtomicFieldUpdateRequest = Body(...),
    session=Depends(get_db_session),
):
    """
    Atomic find-or-create with atomic field update (upsert mode).

    This endpoint atomically:
    1. Ensures the context exists with correct unique_keys configuration
    2. Acquires an advisory lock on the unique key values (prevents race on first insert)
    3. Finds an existing log by unique_keys or creates it with initial_data
    4. Applies an atomic operation to the specified field
    5. If add_to_all_context=true, mirrors the log to the All/* archive context

    Required body fields for upsert mode:
    - project: Name of the project
    - context: Context path for the log
    - unique_keys: Key name to type mapping (e.g., {"_assistant_id": "str", "month": "str"})
    - initial_data: Data for new log entry (must include all unique key values)
    - operation: Atomic operation (+N, -N, *N, /N)

    This is race-safe for concurrent first inserts - only one request will create the log,
    and all concurrent requests will correctly increment the field.
    """
    # Validate upsert mode has required fields
    if (
        not body.project
        or not body.context
        or not body.unique_keys
        or not body.initial_data
    ):
        raise HTTPException(
            status_code=400,
            detail="Upsert mode requires: project, context, unique_keys, and initial_data.",
        )

    return _atomic_field_update_impl(
        request_fastapi=request_fastapi,
        log_id=None,
        field_name=None,
        body=body,
        session=session,
    )


def _atomic_field_update_impl(
    request_fastapi: Request,
    log_id: Optional[int],
    field_name: Optional[str],
    body: AtomicFieldUpdateRequest,
    session,
) -> AtomicFieldUpdateResponse:
    """Internal implementation for atomic field updates.

    Supports two modes:
    1. Update mode: log_id and field_name are provided
    2. Upsert mode: body contains project/context/unique_keys/initial_data
    """
    user_id = request_fastapi.state.user_id
    organization_id = getattr(request_fastapi.state, "organization_id", None)

    # Parse and validate the operation
    match = re.match(r"^([+\-*/])(\d+\.?\d*)$", body.operation)
    if not match:
        raise HTTPException(
            status_code=400,
            detail="Invalid operation format. Use +N, -N, *N, /N where N is a number.",
        )

    operator, operand_str = match.groups()
    operand = float(operand_str)

    # Validate division by zero
    if operator == "/" and operand == 0:
        raise HTTPException(
            status_code=400,
            detail="Division by zero is not allowed.",
        )

    # Determine mode based on parameters
    is_upsert_mode = body.project is not None and body.context is not None

    if is_upsert_mode:
        # === UPSERT MODE ===
        return _atomic_upsert_mode(
            user_id=user_id,
            organization_id=organization_id,
            body=body,
            operator=operator,
            operand=operand,
            session=session,
        )
    else:
        # === UPDATE MODE ===
        if log_id is None or field_name is None:
            raise HTTPException(
                status_code=400,
                detail="Update mode requires log_id and field_name in the path.",
            )

        # Check that the log exists and user has permission
        log_event = session.query(LogEvent).filter(LogEvent.id == log_id).first()
        if not log_event:
            raise HTTPException(status_code=404, detail="Log not found.")

        # Verify user has access to this log's project
        project_dao = ProjectDAO(
            session,
            OrganizationMemberDAO(session),
            ContextDAO(session),
        )
        project = project_dao.filter_by_user_access(
            user_id=user_id,
            id=log_event.project_id,
        )
        if not project:
            raise HTTPException(status_code=404, detail="Log not found.")
        _check_project_write_permission(
            session,
            user_id,
            organization_id,
            log_event.project_id,
        )

        # Build the atomic SQL update
        sql = text(
            f"""
            UPDATE log_event
            SET data = jsonb_set(
                COALESCE(data, '{{}}'::jsonb),
                :path,
                to_jsonb(COALESCE((data->>:field)::numeric, 0) {operator} :operand)
            ),
            updated_at = :now
            WHERE id = :log_id
            RETURNING (data->>:field)::numeric as new_value
            """,
        )

        result = session.execute(
            sql,
            {
                "log_id": log_id,
                "field": field_name,
                "path": "{" + field_name + "}",
                "operand": operand,
                "now": datetime.now(timezone.utc),
            },
        ).fetchone()

        if not result:
            raise HTTPException(status_code=404, detail="Log not found.")

        session.commit()

        return AtomicFieldUpdateResponse(new_value=result.new_value)


def _atomic_upsert_mode(
    user_id: str,
    organization_id: Optional[int],
    body: AtomicFieldUpdateRequest,
    operator: str,
    operand: float,
    session,
) -> AtomicFieldUpdateResponse:
    """Handle atomic upsert mode - find or create log, then apply operation."""
    # Validate that all unique keys are present in initial_data
    for key_name in body.unique_keys.keys():
        if key_name not in body.initial_data:
            raise HTTPException(
                status_code=400,
                detail=f"Missing unique key '{key_name}' in initial_data.",
            )

    # Get or verify project access
    project_dao = ProjectDAO(
        session,
        OrganizationMemberDAO(session),
        ContextDAO(session),
    )
    projects = project_dao.filter_by_user_access(
        user_id=user_id,
        organization_id=organization_id,
        name=body.project,
    )
    if not projects:
        raise HTTPException(status_code=404, detail="Project not found.")
    project = projects[0][0]  # filter_by_user_access returns list of tuples
    project_id = project.id
    _check_project_write_permission(session, user_id, organization_id, project_id)

    context_dao = ContextDAO(session)

    # Create/get context FIRST - this commits internally which is fine
    # The context needs to be visible to all concurrent transactions
    context_id = context_dao.get_or_create(
        project_id=project_id,
        name=body.context,
        unique_keys=body.unique_keys,
    )

    # Build the unique key filter conditions for SQL
    unique_key_conditions = []
    unique_key_values = []
    for key_name in body.unique_keys.keys():
        key_value = body.initial_data[key_name]
        unique_key_conditions.append(f"data->>'{key_name}' = :key_{key_name}")
        unique_key_values.append((f"key_{key_name}", str(key_value)))

    # Build a hash for the advisory lock from project + context + unique key values
    # Include context_id to ensure we're locking on the same context
    lock_key_parts = [str(project_id), str(context_id)]
    for key_name in sorted(body.unique_keys.keys()):
        lock_key_parts.append(str(body.initial_data[key_name]))
    lock_key_str = ":".join(lock_key_parts)

    # Acquire advisory lock on unique key combination
    # This serializes log operations for the same context+unique_keys
    # The lock is held until this transaction commits (after log insert/update)
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
        {"lock_key": lock_key_str},
    )

    # Build the conditions string
    conditions_str = " AND ".join(unique_key_conditions)

    # Check if the log exists
    check_sql = text(
        f"""
        SELECT le.id, le.data
        FROM log_event le
        JOIN log_event_context lec ON lec.log_event_id = le.id
        WHERE le.project_id = :project_id
          AND lec.context_id = :context_id
          AND {conditions_str}
        FOR UPDATE
        """,
    )

    check_params = {
        "project_id": project_id,
        "context_id": context_id,
    }
    for param_name, param_value in unique_key_values:
        check_params[param_name] = param_value

    existing = session.execute(check_sql, check_params).fetchone()

    # Use explicitly provided field name, or fall back to inference for backwards compat
    field_name = body.field
    if field_name is None:
        # Legacy behavior: infer from initial_data (first numeric non-unique-key field)
        for key, value in body.initial_data.items():
            if key not in body.unique_keys and isinstance(value, (int, float)):
                field_name = key
                break

        # If no numeric field found, use the first non-unique-key field
        if field_name is None:
            for key in body.initial_data.keys():
                if key not in body.unique_keys:
                    field_name = key
                    break

    if field_name is None:
        raise HTTPException(
            status_code=400,
            detail="Could not determine field to update. Provide 'field' parameter or include a non-unique-key field in initial_data.",
        )

    # Validate field name for SQL injection
    if '"' in field_name or "'" in field_name or "--" in field_name:
        raise HTTPException(
            status_code=400,
            detail="Invalid field name.",
        )

    if existing:
        # Update existing log
        update_sql = text(
            f"""
            UPDATE log_event
            SET data = jsonb_set(
                    COALESCE(data, '{{}}'::jsonb),
                    :path,
                    to_jsonb(COALESCE((data->>'{field_name}')::numeric, 0) {operator} :operand)
                ),
                updated_at = :now
            WHERE id = :log_id
            RETURNING id, (data->>'{field_name}')::numeric as new_value
            """,
        )

        result = session.execute(
            update_sql,
            {
                "log_id": existing.id,
                "path": "{" + field_name + "}",
                "operand": operand,
                "now": datetime.now(timezone.utc),
            },
        ).fetchone()

        log_id = result.id
        new_value = float(result.new_value)
        created = False
    else:
        # Insert new log
        initial_data_with_field = dict(body.initial_data)
        initial_data_with_field[field_name] = operand

        insert_sql = text(
            f"""
            INSERT INTO log_event (project_id, data, created_at, updated_at)
            VALUES (:project_id, CAST(:initial_data AS jsonb), :now, :now)
            RETURNING id, (data->>'{field_name}')::numeric as new_value
            """,
        )

        result = session.execute(
            insert_sql,
            {
                "project_id": project_id,
                "initial_data": json.dumps(initial_data_with_field),
                "now": datetime.now(timezone.utc),
            },
        ).fetchone()

        log_id = result.id
        new_value = float(result.new_value)
        created = True

        # Link to context
        session.execute(
            text(
                """
                INSERT INTO log_event_context (log_event_id, context_id)
                VALUES (:log_id, :context_id)
                """,
            ),
            {"log_id": log_id, "context_id": context_id},
        )

    mirrored_contexts = []

    # If add_to_all_context=true, mirror to archive context
    if body.add_to_all_context:
        context_parts = body.context.split("/")

        if "_user" in body.initial_data and "_assistant" in body.initial_data:
            user_ctx = str(body.initial_data.get("_user", ""))
            assistant_ctx = str(body.initial_data.get("_assistant", ""))

            user_idx = None
            assistant_idx = None
            for i, part in enumerate(context_parts):
                if part == user_ctx and user_idx is None:
                    user_idx = i
                elif part == assistant_ctx and user_idx is not None:
                    assistant_idx = i
                    break

            if user_idx is not None and assistant_idx is not None:
                prefix_parts = context_parts[:user_idx]
                subcontext_parts = context_parts[assistant_idx + 1 :]

                if prefix_parts:
                    archive_context = (
                        "/".join(prefix_parts) + "/All/" + "/".join(subcontext_parts)
                    )
                else:
                    archive_context = "All/" + "/".join(subcontext_parts)

                archive_context_id = context_dao.get_or_create(
                    project_id=project_id,
                    name=archive_context,
                    unique_keys=body.unique_keys,
                )

                existing_link = session.execute(
                    text(
                        """
                        SELECT 1 FROM log_event_context
                        WHERE log_event_id = :log_id AND context_id = :context_id
                        """,
                    ),
                    {"log_id": log_id, "context_id": archive_context_id},
                ).fetchone()

                if not existing_link:
                    session.execute(
                        text(
                            """
                            INSERT INTO log_event_context (log_event_id, context_id)
                            VALUES (:log_id, :context_id)
                            """,
                        ),
                        {"log_id": log_id, "context_id": archive_context_id},
                    )

                mirrored_contexts.append(archive_context)

    session.commit()

    return AtomicFieldUpdateResponse(
        new_value=new_value,
        log_id=log_id,
        created=created,
        mirrored_contexts=mirrored_contexts if mirrored_contexts else None,
    )


def _update_logs(
    request_fastapi: Request,
    body: UpdateLogRequest,
    session,
    organization_member_dao: OrganizationMemberDAO,
    context_dao: ContextDAO,
    project_dao: ProjectDAO,
    field_type_dao: FieldTypeDAO,
    log_event_dao: LogEventDAO,
    log_dao: LogEventDAO,
    derived_log_dao: LogEventDAO,
):
    """
    Log update implementation.

    This function updates logs by modifying the LogEvent.data JSONB column directly.

    Key behaviors:
    - Updates overwrite current values directly (no param versioning)
    - All data stored in LogEvent.data JSONB column
    - No LogEventLog associations
    - Returns modified_keys for derived log recomputation

    Args:
        request_fastapi: The FastAPI request object
        body: The UpdateLogRequest body
        session: The database session
        organization_member_dao: DAO for organization member operations
        context_dao: DAO for context operations
        project_dao: DAO for project operations
        field_type_dao: DAO for field type operations
        log_event_dao: DAO for log event operations
        log_dao: DAO for log operations
        derived_log_dao: DAO for derived log operations

    Returns:
        Dict with info message, failed updates list, and modified_keys list
    """
    from orchestra.web.api.log.utils.logging_utils import enforce_types

    # Get user ID for permission checks
    user_id = request_fastapi.state.user_id
    organization_id = getattr(request_fastapi.state, "organization_id", None)

    # Normalize the logs parameter to get IDs to update
    ids_to_update = []

    # Use body.logs to determine which logs to update
    if hasattr(body, "logs") and body.logs is not None:
        # Check if it's a filter dict and validate required fields
        if isinstance(body.logs, dict):
            if not body.project_name:
                raise HTTPException(
                    status_code=400,
                    detail="When passing a filter dict in `logs`, you must supply `project`.",
                )

            # Get project ID first for filtering
            try:
                project_obj = project_dao.get_by_user_and_name(
                    name=body.project_name,
                    user_id=user_id,
                )
                project_id = project_obj.id
            except (IndexError, AttributeError):
                raise HTTPException(
                    status_code=404,
                    detail=f"Project '{body.project_name}' not found.",
                )
            _check_project_write_permission(
                session,
                user_id,
                organization_id,
                project_id,
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

    # Validate all log IDs upfront using batch query (O(1) instead of O(N))
    not_found_logs = []
    log_id_to_project = {}  # Maps log_id -> project_id
    updated_ids = set()

    # Batch fetch all permissions in a single query
    try:
        log_id_permissions = log_event_dao.get_user_and_project_ids_batch(ids_to_update)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Unexpected error retrieving project info: {e}",
        )

    # Cache for project access checks (avoid repeated checks for same project)
    project_access_cache: Dict[int, bool] = {}

    for log_id in ids_to_update:
        if log_id not in log_id_permissions:
            not_found_logs.append(log_id)
            continue

        project_user_id, project_id = log_id_permissions[log_id]

        # Check permission
        if project_user_id != request_fastapi.state.user_id:
            # Check cache first for project access
            if project_id not in project_access_cache:
                project_obj = project_dao.filter_by_user_access(
                    user_id=user_id,
                    organization_id=organization_id,
                    id=project_id,
                )
                project_access_cache[project_id] = bool(project_obj)

            if not project_access_cache[project_id]:
                not_found_logs.append(log_id)
                continue

        log_id_to_project[log_id] = project_id

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
    _check_project_write_permission(session, user_id, organization_id, project_id)
    project_obj = session.get(Project, project_id)
    project_name = project_obj.name if project_obj else None

    # Fetch context object once for duplicate checks (cache)
    # This is done early to avoid N queries in the duplicate check loop later
    ctx_obj_cache = None  # Will be populated after ctx_id is determined
    pre_sync_task_ids: Set[int] = set()

    # Get or create context - JSONB mode uses single context_id (simplification)
    if body.context:
        if isinstance(body.context, list):
            # Use first context for JSONB mode
            first_ctx = body.context[0]
            if isinstance(first_ctx, str):
                ctx_id = context_dao.get_or_create(
                    project_id,
                    name=first_ctx,
                    description=None,
                    is_versioned=False,
                )
            else:
                ctx_id = context_dao.get_or_create(
                    project_id,
                    name=first_ctx.name,
                    description=first_ctx.description,
                    is_versioned=first_ctx.is_versioned,
                )
        elif isinstance(body.context, str):
            ctx_id = context_dao.get_or_create(
                project_id,
                name=body.context,
                description=None,
                is_versioned=False,
            )
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

    # Populate context object cache for duplicate checks (single query, reused later)
    ctx_obj_cache = None
    if ctx_id is not None:
        ctx_obj_cache = context_dao.session.query(Context).filter_by(id=ctx_id).first()
        if project_name == TASK_MACHINE_PROJECT_NAME and is_task_surface_context_name(
            getattr(ctx_obj_cache, "name", None),
        ):
            pre_sync_task_ids = get_task_ids_for_log_ids(
                session=session,
                project_id=project_id,
                context_name=ctx_obj_cache.name,
                log_event_ids=ids_to_update,
            )

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
    all_flat_updates = []
    all_nested_updates = []
    new_field_types = []
    updates_by_log_id = {}  # For context versioning
    updated_entry_keys: Set[str] = set()  # Track which entry keys are being updated
    failed_updates: List[Dict] = []  # Collect per-log failures
    pending_mutability_updates: Dict[str, bool] = {}  # Batch mutability changes

    # Process entries
    data = body.entries

    for i, log_id in enumerate(ids_to_update):
        # Extract the data for this log. Support both dict and list formats.
        try:
            this_data = data if isinstance(data, dict) else data[i]
        except (IndexError, TypeError):
            if data is None:
                this_data = {}
            else:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Mismatch between number of log ids ({len(ids_to_update)}) and length of "
                        f"entries (got {len(data)}) at log id {log_id}."
                    ),
                )

        if not this_data:
            continue

        # Remove explicit types if provided, which override inferred types.
        this_data = dict(this_data)  # Make a copy to avoid modifying original
        explicit_types = this_data.pop("explicit_types", {})

        # Track this log for context versioning
        updates_by_log_id[log_id] = updates_by_log_id.get(log_id, 0) + 1

        # If only explicit_types are provided, collect mutability updates for batch
        if not this_data:
            for k, v in explicit_types.items():
                mutable_setting = v.get("mutable", True)
                # Accumulate for batch update (will be executed after loop)
                pending_mutability_updates[k] = mutable_setting

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

                # If field doesn't exist, create it
                if not field_result["exists"]:
                    new_field_types.append(
                        {
                            "project_id": project_id,
                            "field_name": base_key,
                            "value": v,
                            "mutable": field_result["mutable"],
                            "unique": field_result["unique"],
                            "field_category": "entry",
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
                        "context_id": ctx_id,
                        "overwrite": body.overwrite,
                        "explicit_types": explicit_types,
                    },
                )

                # Track this update for context versioning
                updated_ids.add((base_key, log_id))
                updated_entry_keys.add(base_key)
            else:
                # This is a flat update, keep it for normal processing
                flat_data[k] = v
                updated_entry_keys.add(k)

        # Process flat updates
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
                        is_param=False,
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
                new_field_types.append(
                    {
                        "project_id": project_id,
                        "field_name": k,
                        "value": v,
                        "mutable": field_result["mutable"],
                        "unique": field_result["unique"],
                        "field_category": "entry",
                        "context_id": ctx_id,
                        "field_type": field_result["field_type"],
                        "enum_values": field_result["enum_values"],
                        "enum_restrict": field_result["enum_restrict"],
                    },
                )

            # JSONB mode: No param versioning - skip get_next_param_version() calls
            # Add to the batch update list
            all_flat_updates.append(
                {
                    "log_event_id": log_id,
                    "key": k,
                    "value": v,
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

    # Batch update mutability for all accumulated fields (single query)
    if pending_mutability_updates:
        try:
            field_type_dao.bulk_update_mutability(
                project_id=project_id,
                context_id=ctx_id,
                field_mutability_map=pending_mutability_updates,
            )
        except Exception as e:
            raise HTTPException(
                status_code=400,
                detail=f"Failed to update mutability for fields: {e}",
            )

    successful_update_ids: Set[int] = set()

    # Apply FK CASCADE and SET NULL actions before updates
    if ctx_id and all_flat_updates:
        # Determine which columns are being updated
        columns_being_updated = set(u["key"] for u in all_flat_updates)
        log_ids_being_updated = list(set(u["log_event_id"] for u in all_flat_updates))

        # Get OLD values for these columns from logs being updated
        columns_values_map: Dict[str, List[Any]] = {}
        for log_id in log_ids_being_updated:
            log_event = (
                session.query(LogEvent.data).filter(LogEvent.id == log_id).one_or_none()
            )
            if log_event and log_event.data:
                for key in columns_being_updated:
                    if key in log_event.data and log_event.data[key] is not None:
                        columns_values_map.setdefault(key, []).append(
                            log_event.data[key],
                        )

        # Apply FK actions (CASCADE UPDATE, SET NULL)
        if columns_values_map:
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

    # First, handle flat updates using JSONB method
    if all_flat_updates:
        # Enforce unique field constraints for updated values (JSONB mode)
        if ctx_id is not None:
            from orchestra.db.dao.unique_constraint_dao import UniqueConstraintDAO

            unique_fields = {
                k
                for k, v in field_types.items()
                if isinstance(v, dict) and v.get("unique", False)
            }

            if unique_fields:
                unique_dao = UniqueConstraintDAO(session)

                # Group updates by log_event_id to build log_data dicts
                updates_by_log: Dict[int, Dict[str, Any]] = {}
                for update in all_flat_updates:
                    log_id = update.get("log_event_id")
                    field_name = update.get("key")
                    value = update.get("value")
                    if log_id not in updates_by_log:
                        updates_by_log[log_id] = {}
                    updates_by_log[log_id][field_name] = value

                # Prepare log entries for validation
                log_entries = [
                    (log_id, log_data) for log_id, log_data in updates_by_log.items()
                ]

                # Exclude the logs being updated from duplicate check
                exclude_ids = list(updates_by_log.keys())

                # Check for duplicates
                duplicate = unique_dao.check_unique_fields_batch(
                    context_id=ctx_id,
                    project_id=project_id,
                    log_entries=log_entries,
                    unique_fields=unique_fields,
                    exclude_ids=exclude_ids,
                )

                if duplicate:
                    _, field_name, _ = duplicate
                    # Clean up any constraints that were inserted
                    unique_dao.remove_constraints_for_logs(exclude_ids)
                    raise HTTPException(
                        status_code=400,
                        detail=f"Duplicate entry for unique field '{field_name}'.",
                    )

        try:
            # Call bulk_update with all updates
            bulk_result = log_dao.bulk_update(
                all_flat_updates,
                field_types=field_types,
                overwrite=body.overwrite,
            )

            # Add bulk_update failures to our failed_updates list
            failed_updates.extend(bulk_result["failed"])

            # Check for duplicates using batch method (single query for all IDs)
            if (
                ctx_obj_cache
                and not ctx_obj_cache.allow_duplicates
                and bulk_result["successful_update_ids"]
            ):
                # Batch duplicate check - O(1) query instead of O(N)
                duplicate_ids = context_dao.check_for_duplicates_subset_batch(
                    context_id=ctx_id,
                    log_event_ids=bulk_result["successful_update_ids"],
                    keys_to_check=list(updated_entry_keys),
                )
                duplicate_ids_set = set(duplicate_ids)

                for le_id in bulk_result["successful_update_ids"]:
                    if le_id in duplicate_ids_set:
                        failed_updates.append(
                            {
                                "log_event_id": le_id,
                                "error": f"Duplicate log entry detected in context '{ctx_obj_cache.name}'",
                            },
                        )
                    else:
                        successful_update_ids.add(le_id)
            else:
                # No duplicate checking needed, all successful
                successful_update_ids.update(bulk_result["successful_update_ids"])
        except OverwriteError as e:
            failed_updates.append(
                {
                    "log_event_id": le_id,
                    "error": f"Existing value cannot be overwritten because overwrite is set to False: {str(e)}",
                },
            )
        except ImmutableFieldError as e:
            failed_updates.append(
                {
                    "log_event_id": le_id,
                    "error": f"Field is immutable and cannot be modified: {str(e)}",
                },
            )

    # Then, handle nested updates if any exist using JSONB method
    if all_nested_updates:
        # Call apply_jsonb_patch once with all patches - O(1) SELECT/UPDATE
        # The method now handles grouping internally and returns results
        nested_result = log_dao.apply_jsonb_patch(
            all_nested_updates,
            field_types=field_types,
        )

        # Add any failures from nested updates
        failed_updates.extend(nested_result["failed"])

        # Get successful IDs for duplicate check
        nested_successful_ids: List[int] = nested_result["successful_update_ids"]

        # Batch duplicate check for all successful nested updates (single query)
        if nested_successful_ids:
            if ctx_obj_cache and not ctx_obj_cache.allow_duplicates:
                duplicate_ids = context_dao.check_for_duplicates_subset_batch(
                    context_id=ctx_id,
                    log_event_ids=nested_successful_ids,
                    keys_to_check=list(updated_entry_keys),
                )
                duplicate_ids_set = set(duplicate_ids)

                for le_id in nested_successful_ids:
                    if le_id in duplicate_ids_set:
                        failed_updates.append(
                            {
                                "log_event_id": le_id,
                                "error": f"Duplicate log entry detected in context '{ctx_obj_cache.name}'",
                            },
                        )
                    else:
                        successful_update_ids.add(le_id)
            else:
                # No duplicate checking needed
                successful_update_ids.update(nested_successful_ids)

    # Update context version if needed
    if ctx_id is not None:
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
        event_ids = [value for (_, value) in updated_ids]
        _recompute_derived_for_logs(
            session=session,
            project_id=project_id,
            context_id=ctx_id,
            log_ids=event_ids,
            entry_keys=updated_entry_keys,
            derived_log_dao=derived_log_dao,
            field_type_dao=field_type_dao,
        )

    if (
        project_name == TASK_MACHINE_PROJECT_NAME
        and successful_update_ids
        and is_task_surface_context_name(getattr(ctx_obj_cache, "name", None))
    ):
        post_sync_task_ids = get_task_ids_for_log_ids(
            session=session,
            project_id=project_id,
            context_name=ctx_obj_cache.name,
            log_event_ids=successful_update_ids,
        )
        _sync_task_activations_if_needed(
            session=session,
            project_name=project_name,
            project_id=project_id,
            task_ids=pre_sync_task_ids | post_sync_task_ids,
            tasks_context_name=ctx_obj_cache.name,
        )

    # Return response with modified_keys for derived log recomputation
    return {
        "info": "Logs updated successfully!",
        "failed": failed_updates,
        "modified_keys": list(updated_entry_keys),
    }


def _delete_logs(
    session,
    user_id: int,
    project_id: int,
    context_id: int,
    context_name: str,
    ids_and_fields: Dict[Optional[int], List[str]],
    body,
    log_dao: LogEventDAO,
    log_event_dao: LogEventDAO,
    field_type_dao: FieldTypeDAO,
    context_dao: ContextDAO,
    is_assistants_dual_context: bool = False,
):
    """
    Log deletion helper function.

    This function handles log deletions by modifying LogEvent.data JSONB column directly.

    For Assistants/UnityTests projects with 3-tier context hierarchies, this function
    also handles cascading deletions across sibling contexts (All/X, User/All/X,
    User/Assistant/X).

    Args:
        session: Database session
        user_id: The user performing the deletion
        project_id: The project ID
        context_id: The context ID
        context_name: The context name
        ids_and_fields: Dict mapping log_event_id -> list of field names to delete
        body: The DeleteLogEntryRequest body
        log_dao: LogEventDAO instance
        log_event_dao: LogEventDAO instance
        field_type_dao: FieldTypeDAO instance
        context_dao: ContextDAO instance
        is_assistants_dual_context: If True, enables 3-tier context cascade deletion

    Returns:
        Dict with deletion result info
    """
    # Import ARRAY/TEXT types for bulk field removal (data - ARRAY['field1', ...])
    from collections import defaultdict

    from sqlalchemy import cast
    from sqlalchemy.dialects.postgresql import ARRAY, TEXT

    not_found_logs = []
    deleted_fields: Set[str] = set()
    context_updated = False
    context_description = []

    # Handle source_type='derived' - In JSONB mode, derived fields are stored in LogEvent.data
    # We can still delete them by removing the fields from the JSONB column
    # Note: This doesn't distinguish between base and derived fields at the storage level,
    # but the API contract expects to delete the specified fields
    # If source_type='derived' but no fields are specified, we need to know which fields are derived
    if body.source_type == "derived":
        # Get all derived field keys from ActiveDerivedLog templates for this project/context
        derived_field_keys = [
            template.key
            for template in session.query(ActiveDerivedLog)
            .filter(
                ActiveDerivedLog.project_id == project_id,
                ActiveDerivedLog.context_id == context_id,
                ActiveDerivedLog.is_active == True,
            )
            .all()
        ]

        # If specific fields are requested, filter to only derived fields
        # If no fields specified, use all derived fields for the affected logs
        for log_id, fields in list(ids_and_fields.items()):
            if log_id is not None:
                if fields:
                    # Keep only fields that are derived
                    derived_fields_to_delete = [
                        f for f in fields if f in derived_field_keys
                    ]
                    if derived_fields_to_delete:
                        ids_and_fields[log_id] = derived_fields_to_delete
                    else:
                        # No derived fields to delete for this log
                        del ids_and_fields[log_id]
                else:
                    # No specific fields - delete all derived fields for this log
                    ids_and_fields[log_id] = derived_field_keys

    # Get all log_event_ids in this context for validation
    context_log_ids = [
        row[0]
        for row in session.query(LogEventContext.log_event_id)
        .filter(LogEventContext.context_id == context_id)
        .all()
    ]
    pre_sync_task_ids: Set[int] = set()
    if body.project_name == TASK_MACHINE_PROJECT_NAME and is_task_surface_context_name(
        context_name,
    ):
        candidate_task_log_ids: Set[int] = set()
        for log_id, fields in ids_and_fields.items():
            if log_id is None:
                candidate_task_log_ids.update(context_log_ids)
            else:
                candidate_task_log_ids.add(log_id)
        pre_sync_task_ids = get_task_ids_for_log_ids(
            session=session,
            project_id=project_id,
            context_name=context_name,
            log_event_ids=candidate_task_log_ids,
        )

    # Collect all log_event_ids that need media deletion
    all_log_event_ids_for_media = []
    all_field_names_for_media = []

    # =========================================================================
    # Group 1: Global field deletions (log_id is None)
    # =========================================================================
    global_field_deletions = {k: v for k, v in ids_and_fields.items() if k is None}
    for log_id, fields in global_field_deletions.items():
        if len(fields) == 0:
            raise HTTPException(
                status_code=400,
                detail="Cannot delete all logs without specifying fields.",
            )

        # Add fields to the deleted_fields set
        deleted_fields.update(fields)

        # Collect for media deletion
        all_log_event_ids_for_media.extend(context_log_ids)
        all_field_names_for_media.extend(fields)

        # Apply FK CASCADE and SET NULL actions before deletion
        columns_values_to_delete: Dict[str, List[Any]] = {}
        logs_data = (
            session.query(LogEvent.data).filter(LogEvent.id.in_(context_log_ids)).all()
        )
        for (data,) in logs_data:
            if data:
                for key in fields:
                    if key in data and data[key] is not None:
                        columns_values_to_delete.setdefault(key, []).append(data[key])

        # Apply FK actions (CASCADE DELETE, SET NULL)
        if columns_values_to_delete:
            context_dao.apply_fk_actions(
                project_id=project_id,
                context_id=context_id,
                columns_values=columns_values_to_delete,
                action="DELETE",
            )

        # Delete GCS files BEFORE any DB operations
        log_dao._bulk_delete_gcs_media(
            log_event_ids=context_log_ids,
            project_id=project_id,
            field_names=fields,
        )

        # Remove ALL fields from LogEvent.data in a SINGLE UPDATE using array subtraction
        # PostgreSQL: data - ARRAY['field1', 'field2', ...] removes multiple keys at once
        fields_array = cast(fields, ARRAY(TEXT))
        deleted_count = (
            session.query(LogEvent)
            .filter(
                LogEvent.project_id == project_id,
                LogEvent.id.in_(context_log_ids),
            )
            .update(
                {LogEvent.data: LogEvent.data.op("-")(fields_array)},
                synchronize_session=False,
            )
        )
        if deleted_count > 0:
            context_description.append(
                f"Deleted {len(fields)} field(s) from {deleted_count} logs (JSONB)",
            )
            context_updated = True

    # =========================================================================
    # Group 2: Entire log event deletions (fields is empty i.e. passed in as None)
    # =========================================================================
    entire_log_deletion_candidates = [
        log_id
        for log_id, fields in ids_and_fields.items()
        if log_id is not None and len(fields) == 0
    ]

    # Validate ownership in a single query (same pattern as Group 3).
    # For personal projects Project.user_id matches user_id; for org projects
    # Project.user_id is NULL but Project.id == project_id still validates
    # that the log belongs to the correct (already-authorised) project.
    if entire_log_deletion_candidates:
        valid_entire_ids = set(
            row[0]
            for row in session.query(LogEvent.id)
            .filter(
                LogEvent.id.in_(entire_log_deletion_candidates),
                LogEvent.project_id == project_id,
            )
            .all()
        )
    else:
        valid_entire_ids = set()

    entire_log_deletions = []
    for log_id in entire_log_deletion_candidates:
        if log_id in valid_entire_ids:
            entire_log_deletions.append(log_id)
        else:
            not_found_logs.append(log_id)

    if entire_log_deletions:
        # In JSONB mode, source_type='derived' without specific fields is not allowed
        # because JSONB mode doesn't distinguish between base and derived fields at storage level
        if body.source_type == "derived":
            raise HTTPException(
                status_code=400,
                detail="JSONB mode does not distinguish between base and derived fields "
                "at storage level. Cannot delete derived logs without specifying which "
                "derived fields to delete.",
            )

        # Get all field types and add to deleted_fields set
        deleted_fields.update(
            field_type_dao.get_field_types(
                project_id,
                context_id=context_id,
                return_mutable=True,
            ).keys(),
        )

        # Delete GCS files for entire log deletions
        log_dao._bulk_delete_gcs_media(
            log_event_ids=entire_log_deletions,
            project_id=project_id,
            field_names=None,  # Check all media fields
        )

        # Get sibling context IDs for 3-tier context cascade (Assistants/UnityTests)
        sibling_context_map: Dict[int, List[int]] = {}
        if is_assistants_dual_context:
            sibling_context_map = _get_assistants_sibling_context_info(
                session=session,
                project_id=project_id,
                context_id=context_id,
                context_name=context_name,
                log_event_ids=entire_log_deletions,
                context_dao=context_dao,
            )

        # Partition logs: those in other contexts vs those to delete entirely
        # For 3-tier projects, sibling contexts don't count as "other" contexts
        logs_in_other_contexts = []
        logs_to_delete = []

        for log_id in entire_log_deletions:
            exclude_context_ids = [context_id] + sibling_context_map.get(log_id, [])

            other_contexts = (
                session.query(LogEventContext.context_id)
                .filter(
                    LogEventContext.log_event_id == log_id,
                    LogEventContext.context_id.notin_(exclude_context_ids),
                )
                .all()
            )

            if other_contexts:
                logs_in_other_contexts.append(log_id)
            else:
                logs_to_delete.append(log_id)

        # Remove logs from current context
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

        # Cascade deletion to sibling contexts (3-tier hierarchy)
        if is_assistants_dual_context and sibling_context_map:
            sibling_removals = [
                log_id
                for log_id in logs_in_other_contexts
                if log_id in sibling_context_map
            ]
            if sibling_removals:
                sibling_ctx_to_logs: Dict[int, List[int]] = {}
                for log_id in sibling_removals:
                    for sib_ctx_id in sibling_context_map[log_id]:
                        sibling_ctx_to_logs.setdefault(sib_ctx_id, []).append(log_id)

                for sib_ctx_id, log_ids in sibling_ctx_to_logs.items():
                    sibling_removed = (
                        session.query(LogEventContext)
                        .filter(
                            LogEventContext.log_event_id.in_(log_ids),
                            LogEventContext.context_id == sib_ctx_id,
                        )
                        .delete(synchronize_session=False)
                    )
                    if sibling_removed > 0:
                        sib_ctx = context_dao.filter(
                            project_id=project_id,
                            id=sib_ctx_id,
                        )
                        sib_ctx_name = (
                            sib_ctx[0][0].name if sib_ctx else f"id={sib_ctx_id}"
                        )
                        context_description.append(
                            f"Removed {sibling_removed} log events from sibling context '{sib_ctx_name}'",
                        )

        # Apply FK CASCADE and SET NULL actions before deletion
        if logs_to_delete:
            # Collect all field values from logs being deleted for FK cascade
            columns_values_to_delete: Dict[str, List[Any]] = {}
            logs_data = (
                session.query(LogEvent.data)
                .filter(LogEvent.id.in_(logs_to_delete))
                .all()
            )
            for (data,) in logs_data:
                if data:
                    for key, value in data.items():
                        if value is not None:
                            columns_values_to_delete.setdefault(key, []).append(value)

            # Apply FK actions (CASCADE DELETE, SET NULL)
            if columns_values_to_delete:
                context_dao.apply_fk_actions(
                    project_id=project_id,
                    context_id=context_id,
                    columns_values=columns_values_to_delete,
                    action="DELETE",
                )

        # Delete logs that don't exist in other contexts
        if logs_to_delete:
            # Embedding cleanup before hard delete (see LogEventDAO.delete)
            from orchestra.db.dao.embedding_dao import EmbeddingDAO

            embedding_dao = EmbeddingDAO(session)
            embedding_dao.cancel_queue(
                log_event_ids=logs_to_delete,
                reason="Log deleted",
            )
            embedding_dao.soft_delete(log_event_ids=logs_to_delete)
            embedding_dao.null_ref_ids(log_event_ids=logs_to_delete)

            deleted_count = (
                session.query(LogEvent)
                .filter(LogEvent.id.in_(logs_to_delete))
                .delete(synchronize_session=False)
            )
            if deleted_count > 0:
                context_description.append(
                    f"Deleted {deleted_count} log events completely (JSONB)",
                )
                context_updated = True

    # =========================================================================
    # Group 3: Partial field deletions (specific fields for specific log events)
    # =========================================================================
    partial_deletions = {
        k: v for k, v in ids_and_fields.items() if k is not None and len(v) > 0
    }

    potential_empty_logs = []

    if partial_deletions:
        # BULK validate user ownership for all log_ids in a SINGLE QUERY
        partial_log_ids = list(partial_deletions.keys())

        # Query all LogEvents to get their project ownership in one query
        valid_log_ids = set(
            row[0]
            for row in session.query(LogEvent.id)
            .filter(
                LogEvent.id.in_(partial_log_ids),
                LogEvent.project_id == project_id,  # Validates ownership via project
            )
            .all()
        )

        # Identify invalid/not-found logs
        for log_id in partial_log_ids:
            if log_id not in valid_log_ids:
                not_found_logs.append(log_id)

        # Filter to only valid logs
        valid_partial_deletions = {
            k: v for k, v in partial_deletions.items() if k in valid_log_ids
        }

        if valid_partial_deletions:
            # Collect all fields being deleted
            all_partial_fields = set()
            for fields in valid_partial_deletions.values():
                all_partial_fields.update(fields)
                deleted_fields.update(fields)

            # Add all valid log_ids to potential empty logs
            potential_empty_logs = list(valid_partial_deletions.keys())

            # Apply FK CASCADE and SET NULL actions before deletion
            columns_values_to_delete: Dict[str, List[Any]] = {}
            logs_data = (
                session.query(LogEvent.id, LogEvent.data)
                .filter(LogEvent.id.in_(potential_empty_logs))
                .all()
            )
            for log_id, data in logs_data:
                if data:
                    fields_for_this_log = valid_partial_deletions.get(log_id, [])
                    for key in fields_for_this_log:
                        if key in data and data[key] is not None:
                            columns_values_to_delete.setdefault(key, []).append(
                                data[key],
                            )

            # Apply FK actions (CASCADE DELETE, SET NULL)
            if columns_values_to_delete:
                context_dao.apply_fk_actions(
                    project_id=project_id,
                    context_id=context_id,
                    columns_values=columns_values_to_delete,
                    action="DELETE",
                )

            # Delete GCS files BEFORE any DB operations - SINGLE BULK CALL
            log_dao._bulk_delete_gcs_media(
                log_event_ids=potential_empty_logs,
                project_id=project_id,
                field_names=list(all_partial_fields),
            )

            # Group updates by field set for efficient batching
            # {frozenset(fields): [log_ids]} - logs with same fields can be updated together
            fields_to_logs = defaultdict(list)
            for log_id, fields in valid_partial_deletions.items():
                fields_to_logs[frozenset(fields)].append(log_id)

            # Execute BULK UPDATEs - one per unique field set
            for field_set, log_ids in fields_to_logs.items():
                fields_list = list(field_set)
                fields_array = cast(fields_list, ARRAY(TEXT))
                session.query(LogEvent).filter(LogEvent.id.in_(log_ids)).update(
                    {LogEvent.data: LogEvent.data.op("-")(fields_array)},
                    synchronize_session=False,
                )

            context_updated = True
            context_description.append(
                f"Deleted fields from {len(potential_empty_logs)} log events (JSONB)",
            )

    # =========================================================================
    # Delete empty log events if requested (check if data = '{}')
    # =========================================================================
    if body.delete_empty_logs and potential_empty_logs:
        # Find logs where data is empty (equals '{}')
        empty_logs = (
            session.query(LogEvent.id)
            .filter(
                LogEvent.id.in_(potential_empty_logs),
                LogEvent.data == {},
            )
            .all()
        )
        empty_log_ids = [row[0] for row in empty_logs]

        if empty_log_ids:
            # Check which logs exist in other contexts using a SINGLE BULK QUERY
            logs_with_other_contexts = set(
                row[0]
                for row in session.query(LogEventContext.log_event_id)
                .filter(
                    LogEventContext.log_event_id.in_(empty_log_ids),
                    LogEventContext.context_id != context_id,
                )
                .distinct()
                .all()
            )

            # Partition logs based on whether they exist in other contexts
            logs_in_other_contexts = [
                log_id for log_id in empty_log_ids if log_id in logs_with_other_contexts
            ]
            logs_to_delete = [
                log_id
                for log_id in empty_log_ids
                if log_id not in logs_with_other_contexts
            ]

            # Remove logs from this context only - BULK DELETE
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

            # Delete logs that don't exist in other contexts - BULK DELETE
            if logs_to_delete:
                # Embedding cleanup before hard delete (see LogEventDAO.delete)
                from orchestra.db.dao.embedding_dao import EmbeddingDAO

                embedding_dao = EmbeddingDAO(session)
                embedding_dao.cancel_queue(
                    log_event_ids=logs_to_delete,
                    reason="Log deleted",
                )
                embedding_dao.soft_delete(log_event_ids=logs_to_delete)
                embedding_dao.null_ref_ids(log_event_ids=logs_to_delete)

                deleted_count = (
                    session.query(LogEvent)
                    .filter(LogEvent.id.in_(logs_to_delete))
                    .delete(synchronize_session=False)
                )
                if deleted_count > 0:
                    context_description.append(
                        f"Deleted {deleted_count} empty log events completely (JSONB)",
                    )
                    context_updated = True

    # =========================================================================
    # Handle versioned contexts - update timestamp after all deletions
    # =========================================================================
    if context_updated and context_id:
        context_obj = (
            context_dao.session.query(Context).filter_by(id=context_id).first()
        )
        if context_obj:
            context_obj.updated_at = datetime.now(timezone.utc)

    # =========================================================================
    # Handle not found logs
    # =========================================================================
    if not_found_logs:
        raise HTTPException(
            status_code=404,
            detail=f"Logs with ids {not_found_logs} not found or you don't have permission to delete them.",
        )

    # =========================================================================
    # Field type cleanup (check if fields exist in any LogEvent.data)
    # =========================================================================
    if deleted_fields and body.delete_empty_fields:
        # Preserve project-wide shared-log semantics without expanding every
        # JSONB key in the project. We only need to know which deleted fields no
        # longer exist anywhere in the project.
        fields_to_delete_result = session.execute(
            text(
                """
                WITH candidate_fields(field_name) AS (
                    SELECT unnest(CAST(:deleted_fields AS text[]))
                )
                SELECT cf.field_name
                FROM candidate_fields cf
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM log_event le
                    WHERE le.project_id = :project_id
                      AND le.data ? cf.field_name
                    LIMIT 1
                )
                """,
            ),
            {
                "project_id": project_id,
                "deleted_fields": sorted(deleted_fields),
            },
        )

        # Fields returned here no longer exist in any LogEvent.data row for the
        # project, so their context-specific field metadata can be removed.
        fields_to_delete = {row[0] for row in fields_to_delete_result.fetchall()}

        # Delete orphaned field types
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

        _sync_task_activations_if_needed(
            session=session,
            project_name=body.project_name,
            project_id=project_id,
            task_ids=pre_sync_task_ids,
            tasks_context_name=context_name,
        )

    return {"info": "Logs and fields deleted successfully!"}


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
    log_dao = LogEventDAO(session, context_dao)

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
    organization_id = getattr(request_fastapi.state, "organization_id", None)
    try:
        project_id = project_dao.get_by_user_and_name(
            user_id=user_id,
            name=body.project_name,
            organization_id=organization_id,
        ).id
    except (IndexError, AttributeError):
        raise HTTPException(
            status_code=404,
            detail=f"Project '{body.project_name}' not found.",
        )
    _check_project_write_permission(session, user_id, organization_id, project_id)

    # Validate context
    context_name = body.context if body.context else ""
    context = context_dao.filter(project_id=project_id, name=context_name)
    if not context:
        raise HTTPException(
            status_code=404,
            detail=f"Context '{context_name}' not found for project '{body.project_name}'.",
        )
    context_id = context[0][0].id

    # Detect Assistants project dual-context pattern
    # When project is "Assistants" or "UnityTests", logs exist in both "All/<SubContext>"
    # and "{user_id}/{assistant_id}/<SubContext>" contexts. Deleting from one should also
    # delete from the sibling context.
    is_assistants_dual_context = (
        (body.project_name == "Assistants" or "UnityTests" in body.project_name)
        and context_name
        and "/" in context_name
    )

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

    # Use JSONB-based deletion path
    return _delete_logs(
        session=session,
        user_id=user_id,
        project_id=project_id,
        context_id=context_id,
        context_name=context_name,
        ids_and_fields=ids_and_fields,
        body=body,
        log_dao=log_dao,
        log_event_dao=log_event_dao,
        field_type_dao=field_type_dao,
        context_dao=context_dao,
        is_assistants_dual_context=is_assistants_dual_context,
    )


@router.get(
    "/logs",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {
                        "logs": [
                            {
                                "id": "0",
                                "ts": "2024-10-30 12:20:03",
                                "entries": {
                                    "key1": "a",
                                    "key2": 1.0,
                                },
                                "derived_entries": {},
                            },
                            {
                                "id": "1",
                                "ts": "2024-10-30 12:22:14",
                                "entries": {
                                    "key1": "b",
                                    "key2": 2.0,
                                },
                                "derived_entries": {},
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
    project_name: str = Query(
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
        description="Boolean string to filter entries.",
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

    organization_id = getattr(request_fastapi.state, "organization_id", None)
    try:
        project_id = project_dao.get_by_user_and_name(
            name=project_name,
            user_id=request_fastapi.state.user_id,
            organization_id=organization_id,
        ).id
    except Exception as e:
        raise HTTPException(
            status_code=404,
            detail=f"Project {project_name} not found.",
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
        # JSONB query path
        try:
            import time

            start_time = time.time()
            rows, total_count = _get_logs_query(
                request_fastapi,
                project_name=project_name,
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

            # Handle return_ids_only mode
            if return_ids_only:
                return [
                    row[0] for row in rows
                ]  # Extract IDs from (id, data, created_at) tuples

            # Get field metadata for formatting
            field_types = field_type_dao.get_field_types(
                project_id,
                context_id=context_id,
                return_mutable=True,
            )
            field_order_map = field_type_dao.get_ordered_field_names(
                project_id,
                context_id=context_id,
            )

            # Format JSONB results
            logs_out, _ = _format_logs(
                rows=rows,
                field_types=field_types,
                value_limit=value_limit,
                column_context=column_context,
                field_order_map=field_order_map,
                from_fields=from_fields,
                exclude_fields=exclude_fields,
            )

            # Apply grouping of repeated fields if group_threshold is set
            grouped_entries = {}
            if group_threshold is not None and group_threshold > 0:
                logs_out, grouped_entries = apply_group_threshold(
                    logs_out,
                    group_threshold,
                )

            # When field filters are applied, some logs may be filtered out
            # Use actual log count in that case
            actual_count = (
                len(logs_out) if (from_fields or exclude_fields) else total_count
            )

            response = {
                "logs": logs_out,
                "count": actual_count,
            }
            if grouped_entries:
                response["grouped_entries"] = grouped_entries

            return response

        except DataError as e:
            error_msg = _sanitize_sql_error(e)
            raise HTTPException(
                status_code=400,
                detail=f"Invalid data format in filter: {error_msg}",
            )
        except SQLAlchemyError as e:
            error_msg = _sanitize_sql_error(e)
            raise HTTPException(
                status_code=500,
                detail=f"Database error: {error_msg}",
            )

    # -----------------------------------------------------------
    # Stage 2: Grouping Case
    #   (a) Retrieve all matching log event IDs (ignoring limit/offset)
    # -----------------------------------------------------------
    try:
        event_ids_subq, total_count = _get_all_filtered_log_event_ids(
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
            logs_out, _ = _format_flat_logs(
                rows,
                context_len,
                value_limit,
                field_order_map,
            )

            groups = {}

            def parse_group_key(key: str) -> Tuple[str, str]:
                parts = key.split("/", 1)
                return (parts[0], parts[1]) if len(parts) == 2 else ("", key)

            for group_field in group_by:
                prefix, raw_key = parse_group_key(group_field)
                # Note: params prefix is no longer used, all fields are entries now
                distinct_values = _get_distinct_group_values(
                    log_event_ids=event_ids_subq,
                    group_key=raw_key,
                    session=session,
                    field_types={},
                )
                value_to_ids = {}
                used_ids = set()
                for val in distinct_values:
                    subset_ids = _get_log_event_ids_for_group_value(
                        log_event_ids=event_ids_subq,
                        group_key=raw_key,
                        group_value=val,
                        session=session,
                        field_types={},
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
                    paged_keys = all_keys_sorted[
                        group_offset : group_offset + group_limit
                    ]
                else:
                    paged_keys = all_keys_sorted
                paged_mapping = {k: value_to_ids[k] for k in paged_keys}
                field_total = sum(len(ids) for ids in value_to_ids.values())
                groups[group_field] = {
                    **paged_mapping,
                    "group_count": total_distinct,
                    "count": field_total,
                }

            if groups_only:
                logs_out = []

            final_result = {
                "groups": groups,
                "logs": logs_out,
                "count": total_count,
            }

        # -----------------------------------------------------------
        # Stage 5: Return the Final Result.
        # -----------------------------------------------------------
        return final_result
    except DataError as e:
        # Handle data format errors (e.g., invalid datetime casts)
        error_msg = _sanitize_sql_error(e)
        raise HTTPException(
            status_code=400,
            detail=f"Invalid data format in filter: {error_msg}",
        )
    except SQLAlchemyError as e:
        # Handle other SQLAlchemy errors
        error_msg = _sanitize_sql_error(e)
        raise HTTPException(
            status_code=500,
            detail=f"Database error: {error_msg}",
        )


@router.post(
    "/logs/query",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {
                        "logs": [
                            {
                                "id": "0",
                                "ts": "2024-10-30 12:20:03",
                                "entries": {
                                    "key1": "a",
                                    "key2": 1.0,
                                },
                                "derived_entries": {},
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
        "project_name": "my-project",
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
    organization_id = getattr(request_fastapi.state, "organization_id", None)
    try:
        project_id = project_dao.get_by_user_and_name(
            name=body.project_name,
            user_id=request_fastapi.state.user_id,
            organization_id=organization_id,
        ).id
    except Exception as e:
        raise HTTPException(
            status_code=404,
            detail=f"Project {body.project_name} not found.",
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
        # JSONB query path
        rows, total_count = _get_logs_query(
            request_fastapi,
            project_name=body.project_name,
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

        # Handle return_ids_only mode
        if body.return_ids_only:
            return {
                "logs": [row[0] for row in rows],
                "count": total_count,
            }

        # Get field metadata for formatting
        field_types = field_type_dao.get_field_types(
            project_id,
            context_id=context_id,
            return_mutable=True,
        )
        field_order_map = field_type_dao.get_ordered_field_names(
            project_id,
            context_id=context_id,
        )

        # Format JSONB results
        logs_out, _ = _format_logs(
            rows=rows,
            field_types=field_types,
            value_limit=body.value_limit,
            column_context=body.column_context,
            field_order_map=field_order_map,
            from_fields=body.from_fields,
            exclude_fields=body.exclude_fields,
        )

        # Apply group threshold if needed
        if body.group_threshold:
            logs_out = apply_group_threshold(logs_out, body.group_threshold)

        # When field filters are applied, some logs may be filtered out
        actual_count = (
            len(logs_out) if (body.from_fields or body.exclude_fields) else total_count
        )

        return {
            "logs": logs_out,
            "count": actual_count,
        }
    else:
        # Handle grouped case - similar to GET /logs grouped logic
        all_rows, context_len, total_count = _get_all_filtered_log_event_ids(
            request_fastapi=request_fastapi,
            project_name=body.project_name,
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
            project_name=body.project_name,
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
                        "logs": [
                            {
                                "id": "0",
                                "ts": "2024-10-30 12:20:03",
                                "entries": {
                                    "key1": "a",
                                    "key2": 1.0,
                                },
                            },
                            {
                                "id": "1",
                                "ts": "2024-10-30 12:22:14",
                                "entries": {
                                    "key1": "b",
                                    "key2": 2.0,
                                },
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
    project_name: str = Query(
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
        description="Boolean string to filter entries.",
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
        project_name=project_name,
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
    project_name: str = Query(...),
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
        organization_id = getattr(request_fastapi.state, "organization_id", None)
        project_obj = project_dao.get_by_user_and_name(
            name=project_name,
            user_id=user_id,
            organization_id=organization_id,
        )
        project_id = project_obj.id
    except (IndexError, AttributeError):
        raise not_found(f"Project {project_name}")

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
    project_name: str = Query(
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

    groups = dict()

    # JSONB mode: returns (id, data_dict, key_order, created_at) tuples
    rows, _ = _get_logs_query(
        request_fastapi=request_fastapi,
        project_name=project_name,
        context=context,
        filter_expr=filter_expr,
        sorting=None,
        from_ids=from_ids,
        exclude_ids=exclude_ids,
        from_fields=key,  # Filter to logs containing this key
        exclude_fields=None,
        limit=None,
        offset=0,
        project_dao=project_dao,
        field_type_dao=field_type_dao,
        context_dao=context_dao,
        session=session,
    )

    # Extract values from JSONB data dict
    for row in rows:
        # row is (id, data_dict, key_order, created_at)
        data_dict = row[1]

        # Try entries first, then top-level
        value = None
        if "entries" in data_dict and key in data_dict["entries"]:
            value = data_dict["entries"][key]
        elif key in data_dict:
            value = data_dict[key]

        if value is None:
            continue

        # Assign sequential version by unique value
        found_match = False
        for k, v in groups.items():
            if value in v:
                found_match = True
                groups[k].add(value)
                break
        if not found_match:
            version = str(len(groups))
            groups[version] = set()
            groups[version].add(value)

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
    log_dao = LogEventDAO(session, context_dao)

    try:
        # Check if this is a protected task-machine context
        _require_mutable_task_machine_context(
            project_name=request.project_name,
            context_name=request.context,
        )

        # Validate project and permissions
        user_id = request_fastapi.state.user_id
        organization_id = getattr(request_fastapi.state, "organization_id", None)
        project = project_dao.get_by_user_and_name(
            user_id=user_id,
            name=request.project_name,
            organization_id=organization_id,
        )

        if not project:
            raise HTTPException(
                status_code=404,
                detail=f"Project '{request.project_name}' not found",
            )
        project_id = project.id
        _check_project_write_permission(session, user_id, organization_id, project_id)

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
        project_name: Name of the project containing the logs

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
    organization_id = getattr(request_fastapi.state, "organization_id", None)
    try:
        project_obj = project_dao.get_by_user_and_name(
            user_id=user_id,
            name=request.project_name,
            organization_id=organization_id,
        )
        project_id = project_obj.id
    except (IndexError, AttributeError):
        raise HTTPException(
            status_code=404,
            detail=f"Project '{request.project_name}' not found.",
        )
    _check_project_write_permission(session, user_id, organization_id, project_id)

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
            project_name=request.project_name,
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


@router.post(
    "/logs/join_query",
    responses={
        200: {
            "description": "Join query executed successfully",
        },
        400: {
            "description": "Bad Request",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Invalid join query parameters.",
                    },
                },
            },
        },
        404: {
            "description": "Project Not Found",
        },
    },
)
def join_query(
    request_fastapi: Request,
    request: JoinQueryRequest,
    session=Depends(get_db_session),
):
    """Execute a join and query or reduce the result without materialisation.

    This endpoint combines ``POST /logs/join`` with ``GET /logs`` or
    ``GET /logs/metric`` into a single operation.  No temporary context
    is created; the join is evaluated as a SQL subquery and the
    filter/sort/paginate/reduce operations are applied on top.
    """
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    field_type_dao = FieldTypeDAO(session)

    user_id = request_fastapi.state.user_id
    organization_id = getattr(request_fastapi.state, "organization_id", None)
    try:
        project_obj = project_dao.get_by_user_and_name(
            user_id=user_id,
            name=request.project_name,
            organization_id=organization_id,
        )
        project_id = project_obj.id
    except (IndexError, AttributeError):
        raise HTTPException(
            status_code=404,
            detail=f"Project '{request.project_name}' not found.",
        )

    if not isinstance(request.pair_of_args, list) or len(request.pair_of_args) != 2:
        raise HTTPException(
            status_code=400,
            detail="pair_of_args must be a list containing exactly two dictionaries.",
        )

    valid_modes = ["inner", "left", "right", "outer"]
    if request.mode not in valid_modes:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid join mode. Must be one of: {', '.join(valid_modes)}",
        )

    if not request.join_expr or not isinstance(request.join_expr, str):
        raise HTTPException(
            status_code=400,
            detail="join_expr must be a non-empty string.",
        )

    is_reduce = request.metric is not None
    if is_reduce:
        if request.key is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "`key` is required when `metric` is set. "
                    "Specify the column(s) to aggregate, e.g. key='amount'."
                ),
            )
        if request.sorting is not None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "`sorting` only applies to row mode (metric=None). "
                    "Reduce mode returns grouped aggregates which have no "
                    "row order. To sort rows before aggregating, apply a "
                    "pre-join filter via pair_of_args instead, or call "
                    "POST /logs/join_query without `metric` to get sorted "
                    "rows, then POST /logs/metric/{metric} on those results."
                ),
            )
        if request.limit is not None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "`limit` only applies to row mode (metric=None). "
                    "Reduce mode returns one value per group — all groups "
                    "are always returned. To limit input rows before "
                    "aggregating, use `filter_expr` or pre-join filters "
                    "in `pair_of_args`."
                ),
            )
        if request.offset != 0:
            raise HTTPException(
                status_code=400,
                detail=(
                    "`offset` only applies to row mode (metric=None). "
                    "Reduce mode returns one value per group — pagination "
                    "does not apply. To paginate joined rows, call this "
                    "endpoint without `metric` and use `limit`/`offset`."
                ),
            )
    else:
        if request.key is not None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "`key` only applies to reduce mode. Set `metric` "
                    "(e.g. 'count', 'sum', 'mean') together with `key` "
                    "to aggregate, or remove `key` to use row mode."
                ),
            )
        if request.group_by is not None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "`group_by` only applies to reduce mode. Set `metric` "
                    "and `key` together with `group_by` to get grouped "
                    "aggregates. For row mode, use `sorting` to order "
                    "results and `limit`/`offset` to paginate."
                ),
            )

    try:
        result = _join_query_internal(
            project_id=project_id,
            project_name=request.project_name,
            pair_of_args=request.pair_of_args,
            join_expr=request.join_expr,
            mode=request.mode,
            columns=request.columns,
            filter_expr=request.filter_expr,
            sorting=request.sorting,
            limit=request.limit,
            offset=request.offset,
            group_by=request.group_by,
            metric=request.metric,
            key=request.key,
            request_fastapi=request_fastapi,
            project_dao=project_dao,
            field_type_dao=field_type_dao,
            context_dao=context_dao,
            session=session,
        )
        return result

    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail=str(e),
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error performing join query: {str(e)}",
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
    project_name: str = Query(
        description="Name of the project to get fields and their types for.",
        example="eval-project",
    ),
    context: Optional[str] = Query(
        "",
        description=(
            "Optional context name to filter field types. "
            "Use '*' to return fields from all contexts in the project."
        ),
        example="training",
    ),
    session=Depends(get_db_session),
):
    """
    Returns field definitions and their types for the specified project.

    Context handling:
    - If no context is provided (or an empty string), returns a flat mapping of field
      name → metadata for the default context.
    - If a specific context name is provided, returns a flat mapping of field
      name → metadata for that context.
    - If context is '*', returns a nested mapping of context_name → {field_name → metadata}
      containing fields from all contexts in the project.

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
        organization_id = getattr(request_fastapi.state, "organization_id", None)
        project_obj = project_dao.get_by_user_and_name(
            name=project_name,
            user_id=user_id,
            organization_id=organization_id,
        )
        project_id = project_obj.id
    except (IndexError, AttributeError):
        raise not_found(f"Project {project_name}")

    # For derived entries, get their equations (project-wide)
    derived_equations = {
        key: equation
        for key, equation in session.query(
            ActiveDerivedLog.key,
            ActiveDerivedLog.equation,
        )
        .filter(ActiveDerivedLog.project_id == project_obj.id)
        .all()
    }

    # Wildcard: return mapping of context_name -> fields
    if context == "*":
        all_contexts = context_dao.filter(project_id=project_obj.id)
        result = {}

        for ctx_row in all_contexts:
            ctx = ctx_row[0]
            ctx_id = ctx.id
            ctx_name = ctx.name

            types = field_type_dao.get_field_types(
                project_obj.id,
                context_id=ctx_id,
                return_mutable=True,
            )
            if not types:
                # Skip contexts with no fields
                continue

            result[ctx_name] = {
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

        return result

    # Non-wildcard: resolve a single context and return flat mapping
    context_id = None
    context_obj = None

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
                        "info": "Fields created successfully. Backfilled 2 log events with None values.",
                        "backfilled_count": 2,
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

    Backfill behavior:
        When ``backfill_logs=True``, the endpoint null-merges any field it has
        not yet stamped into every existing ``log_event`` row of the context.
        Stamping is recorded on ``field_type.backfilled_at``: once a field has
        been backfilled, subsequent idempotent re-POSTs short-circuit at the
        DAO level with a cheap indexed probe, avoiding the full-context scan
        that previously dominated Cloud SQL CPU.

    Response:
        - ``info``: human-readable summary.
        - ``backfilled_count``: number of **log_event rows** whose ``data`` column
          was updated by the backfill. A row counts as updated if it was missing
          at least one of the requested field keys before the call. Rows that
          already contained every requested key are skipped (no write, no WAL)
          and do not contribute to this count. Always ``0`` when
          ``backfill_logs=False``, when ``fields`` is empty, or when every
          requested field is already stamped as backfilled.
    """
    # Instantiate DAOs with shared session
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    field_type_dao = FieldTypeDAO(session)

    # Validate project
    try:
        user_id = request_fastapi.state.user_id
        organization_id = getattr(request_fastapi.state, "organization_id", None)
        project = project_dao.get_by_user_and_name(
            user_id=user_id,
            name=request.project_name,
            organization_id=organization_id,
        )
        project_id = project.id
    except (IndexError, AttributeError):
        raise HTTPException(
            status_code=404,
            detail=f"Project '{request.project_name}' not found.",
        )
    _check_project_write_permission(session, user_id, organization_id, project_id)

    # Get or create context
    context_name = request.context if request.context else ""
    context_id = context_dao.get_or_create(
        project_id=project_id,
        name=context_name,
        description=None,
        is_versioned=False,
    )

    # Create fields and learn which still need a log_event backfill.
    #
    # `create_fields` upserts the field_type rows and RETURNs the subset whose
    # `backfilled_at IS NULL` after the upsert. That subset is the ONLY work
    # the backfill UPDATE below must do; stamped fields can be skipped
    # entirely. This is the primary production fix: ingestion pods re-POST
    # the same field set hundreds of times per context, and without this gate
    # every call forced a full-context scan of `log_event` even when no row
    # needed changing (the `?&` guard suppressed writes but not the scan).
    try:
        pending_backfill_fields = field_type_dao.create_fields(
            project_id=project_id,
            context_id=context_id,
            fields=request.fields,
        )
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Failed to create fields: {str(e)}",
        )

    # Null-merge pending fields into every row of the context, then stamp.
    #
    # `backfilled_count` is `result.rowcount`, i.e. the number of log_event
    # ROWS the UPDATE actually touched. A row is touched iff it was missing
    # at least one of the pending field keys (the `?&` guard filters
    # fully-populated rows out before any write -- this matters for the
    # partial-overlap case where some pending fields are already present on
    # some rows via log-creation side effects).
    #
    # After the UPDATE succeeds we stamp `field_type.backfilled_at = now()`
    # for the pending subset so subsequent idempotent re-POSTs short-circuit
    # before this block executes. Stamp + UPDATE + upsert all commit as one
    # transaction so a crash mid-UPDATE leaves the stamp unset and the next
    # call will retry.
    backfilled_count = 0
    if request.backfill_logs and pending_backfill_fields:
        try:
            template_json = json.dumps(
                {name: None for name in pending_backfill_fields},
            )

            result = session.execute(
                text(
                    """
                    UPDATE log_event le
                    SET data = CAST(:template AS jsonb) || COALESCE(le.data, '{}'::jsonb),
                        updated_at = now()
                    FROM log_event_context lec
                    WHERE lec.log_event_id = le.id
                      AND lec.context_id = :context_id
                      AND le.project_id = :project_id
                      AND NOT (le.data ?& CAST(:field_names AS text[]))
                    """,
                ),
                {
                    "template": template_json,
                    "field_names": pending_backfill_fields,
                    "context_id": context_id,
                    "project_id": project_id,
                },
            )
            backfilled_count = result.rowcount or 0

            field_type_dao.mark_backfilled(
                project_id=project_id,
                context_id=context_id,
                field_names=pending_backfill_fields,
            )
            session.commit()
        except Exception as e:
            session.rollback()
            raise HTTPException(
                status_code=500,
                detail=f"Failed to backfill logs: {str(e)}",
            )

    return {
        "info": f"Fields created successfully. {'Backfilled ' + str(backfilled_count) + ' log events with None values.' if request.backfill_logs and backfilled_count > 0 else ''}",
        "backfilled_count": backfilled_count if request.backfill_logs else 0,
    }


@router.patch(
    "/logs/update_field",
    responses={
        200: {
            "description": "Field updated successfully",
            "content": {
                "application/json": {
                    "example": {
                        "info": "Field updated successfully.",
                    },
                },
            },
        },
        404: {
            "description": "Project, context, or field not found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Field 'score' not found.",
                    },
                },
            },
        },
    },
)
def update_field(
    request_fastapi: Request,
    request: UpdateFieldRequest,
    session=Depends(get_db_session),
):
    """
    Updates an existing field.

    For now this is intentionally metadata-only and only supports updating the
    field description. It does not modify log payloads, derived templates, field
    names, mutability, or data types.
    """
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    field_type_dao = FieldTypeDAO(session)

    _require_mutable_task_machine_context(
        project_name=request.project_name,
        context_name=request.context,
    )

    try:
        user_id = request_fastapi.state.user_id
        organization_id = getattr(request_fastapi.state, "organization_id", None)
        project = project_dao.get_by_user_and_name(
            user_id=user_id,
            name=request.project_name,
            organization_id=organization_id,
        )
        project_id = project.id
    except (IndexError, AttributeError):
        raise HTTPException(
            status_code=404,
            detail=f"Project '{request.project_name}' not found.",
        )
    _check_project_write_permission(session, user_id, organization_id, project_id)

    context_name = request.context or ""
    if request.context:
        context_rows = context_dao.filter(project_id=project_id, name=context_name)
        if not context_rows:
            raise HTTPException(
                status_code=404,
                detail=f"Context '{context_name}' not found.",
            )
        context_id = context_rows[0][0].id
    else:
        context_id = context_dao.get_or_create(
            project_id=project_id,
            name="",
            description=None,
            is_versioned=False,
        )

    try:
        field_type_dao.update_field(
            project_id=project_id,
            field_name=request.field_name,
            context_id=context_id,
            description=request.description,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Failed to update field description: {str(e)}",
        )

    return {"info": "Field updated successfully."}


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
    1. Remove the field from LogEvent.data for all matching log events
    2. Delete the field type records for those fields

    This operation cannot be undone, so use with caution.
    """
    # Instantiate DAOs with shared session
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    field_type_dao = FieldTypeDAO(session)
    log_dao = LogEventDAO(session, context_dao)

    # Check if this is a protected task-machine context
    _require_mutable_task_machine_context(
        project_name=request.project_name,
        context_name=request.context,
    )

    # Validate project
    try:
        user_id = request_fastapi.state.user_id
        organization_id = getattr(request_fastapi.state, "organization_id", None)
        project = project_dao.get_by_user_and_name(
            user_id=user_id,
            name=request.project_name,
            organization_id=organization_id,
        )
        project_id = project.id
    except (IndexError, AttributeError):
        raise HTTPException(
            status_code=404,
            detail=f"Project '{request.project_name}' not found.",
        )
    _check_project_write_permission(session, user_id, organization_id, project_id)

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
    total_updated_events = 0

    for field_name in request.fields:
        try:
            # Get log events where the field exists in LogEvent.data
            jsonb_log_events = (
                session.query(LogEvent.id)
                .join(LogEventContext, LogEventContext.log_event_id == LogEvent.id)
                .filter(
                    LogEvent.project_id == project_id,
                    LogEventContext.context_id == context_id,
                    LogEvent.data.has_key(field_name),
                )
                .distinct()
            )
            event_ids = [event_id[0] for event_id in jsonb_log_events.all()]

            if event_ids:
                # Delete GCS media files before updating database records
                log_dao._bulk_delete_gcs_media(event_ids, project_id, [field_name])

                # Remove the field from LogEvent.data JSONB column
                # This is a single bulk UPDATE - O(1) query regardless of number of log events
                if event_ids:
                    from sqlalchemy import text

                    session.execute(
                        text(
                            """
                            UPDATE log_event
                            SET data = data - :field_name
                            WHERE id = ANY(:event_ids)
                            AND data ? :field_name
                        """,
                        ),
                        {
                            "field_name": field_name,
                            "event_ids": event_ids,
                        },
                    )
                total_updated_events += len(event_ids)

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
        "info": f"Fields deleted successfully. Updated {total_updated_events} log events.",
        "deleted_fields": deleted_fields,
    }


######################
# Admin endpoints
######################

# Derived Log Backfill (update_active_derived_logs cron)
# Non-embedding derived logs are pure SQL (JSONB concat), ~500-1000 logs/sec.
# Sync embedding templates are the bottleneck (~15-20s per 2048-item API batch).
# max_time_seconds is the real safety net for slow embedding templates,
# so max_logs can be generous for the common fast path.
DERIVED_DEFAULT_MAX_LOGS = 10000
DERIVED_MAX_LOGS_HARD_CAP = 50000
DERIVED_DEFAULT_MAX_TIME = 240  # 4 min, fits within 4m30s deadline
DERIVED_MAX_TIME_HARD_CAP = 300


@admin_router.post(
    "/update_active_derived_logs",
    responses={
        200: {
            "description": "Active derived logs updated successfully",
            "content": {
                "application/json": {
                    "example": {
                        "total_templates_processed": 5,
                        "total_templates_skipped": 2,
                        "total_derived_logs_created": 42,
                        "timed_out": False,
                        "cap_reached": False,
                        "elapsed_seconds": 12.34,
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
    max_logs: int = Query(
        DERIVED_DEFAULT_MAX_LOGS,
        le=DERIVED_MAX_LOGS_HARD_CAP,
        description="Maximum total logs to process across all templates. "
        f"Default: {DERIVED_DEFAULT_MAX_LOGS}, Hard cap: {DERIVED_MAX_LOGS_HARD_CAP}.",
    ),
    max_time_seconds: int = Query(
        DERIVED_DEFAULT_MAX_TIME,
        le=DERIVED_MAX_TIME_HARD_CAP,
        description="Maximum processing time in seconds. "
        f"Default: {DERIVED_DEFAULT_MAX_TIME}s, Hard cap: {DERIVED_MAX_TIME_HARD_CAP}s.",
    ),
    dry_run: bool = Query(
        False,
        description="If true, return per-template pending log counts without processing.",
    ),
    session=Depends(get_db_session),
    _=Depends(auth_admin_key),
):
    """
    Backfill missing derived logs for active templates.

    Iterates all active ``ActiveDerivedLog`` templates and finds logs that are
    missing the derived key. For each template, computes and writes derived
    values via ``recompute_derived_logs``.

    **Modes:**
    - ``dry_run=true``: Return per-template pending log counts only, no processing.
    - Default: Process up to ``max_logs`` logs within ``max_time_seconds``.

    **Cloud Scheduler Configuration:**
    - Schedule: ``*/5 * * * *`` (every 5 minutes)
    - URL: ``POST /admin/update_active_derived_logs?max_logs=10000&max_time_seconds=240``
    - Attempt deadline: 4m30s (270s)
    """
    import time

    start_time = time.time()
    field_type_dao = FieldTypeDAO(session)
    context_dao = ContextDAO(session)

    try:
        active_templates = (
            session.query(ActiveDerivedLog)
            .filter(ActiveDerivedLog.is_active == True)
            .all()
        )

        if not active_templates:
            return {
                "total_templates": 0,
                "total_pending_logs": 0,
                "templates": [],
            }

        def _is_embedding_template(template):
            return "embed(" in template.equation or "embed_image(" in template.equation

        def _build_pending_query(template):
            """Build the query for logs missing the derived key.

            For embedding templates, checks the Embedding table (where vectors
            actually live) instead of data JSONB. The /logs/derived +
            generate_pending_embeddings flow writes to Embedding without
            touching log_event.data, so data ? key is the wrong signal.
            """
            is_emb = _is_embedding_template(template)
            q = session.query(LogEvent.id).join(
                LogEventContext,
                LogEventContext.log_event_id == LogEvent.id,
            )
            if is_emb:
                q = q.outerjoin(
                    Embedding,
                    and_(
                        Embedding.ref_id == LogEvent.id,
                        Embedding.key == template.key,
                        Embedding.is_deleted == False,  # noqa: E712
                    ),
                ).filter(
                    LogEvent.project_id == template.project_id,
                    LogEventContext.context_id == template.context_id,
                    Embedding.id.is_(None),
                )
            else:
                q = q.filter(
                    LogEvent.project_id == template.project_id,
                    LogEventContext.context_id == template.context_id,
                    ~LogEvent.data.has_key(template.key),
                )

            if template.filter_expression:
                field_types = field_type_dao.get_field_types(
                    template.project_id,
                    context_id=template.context_id,
                )

                for alias, filter_config in template.filter_expression.items():
                    if (
                        isinstance(filter_config, dict)
                        and "filter_expr" in filter_config
                        and filter_config["filter_expr"]
                    ):
                        try:
                            filter_dict = str_filter_exp_to_dict(
                                filter_config["filter_expr"],
                                field_names=list(field_types.keys()),
                            )
                            condition = build_sql_query(
                                filter_dict,
                                LogEvent,
                                session,
                                log_event_ids=q.subquery(),
                            )

                            if isinstance(condition, Subquery):
                                q = session.query(
                                    LogEvent.id,
                                ).filter(
                                    LogEvent.id.in_(
                                        select(q.subquery().c.id),
                                    ),
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
                        except Exception as filter_error:
                            logging.warning(
                                f"Failed to apply filter for template '{template.key}': {filter_error}",
                            )
            return q

        # --- dry_run mode: return metrics only ---
        # Non-embedding templates: batched conditional aggregation on data ? key
        # Embedding templates: check Embedding table (where vectors live)
        if dry_run:
            from collections import defaultdict

            from sqlalchemy import case
            from sqlalchemy import func as sa_func
            from sqlalchemy import literal

            dry_run_timed_out = False

            # Split and group templates by scope
            scope_non_emb: dict[tuple, list] = defaultdict(list)
            scope_emb: dict[tuple, list] = defaultdict(list)
            for t in active_templates:
                scope_key = (t.project_id, t.context_id)
                if _is_embedding_template(t):
                    scope_emb[scope_key].append(t)
                else:
                    scope_non_emb[scope_key].append(t)

            pending_by_template: dict[int, int] = {}

            # 1) Non-embedding: batched conditional aggregation on data ? key
            for (project_id, context_id), templates in scope_non_emb.items():
                if time.time() - start_time >= max_time_seconds:
                    dry_run_timed_out = True
                    break
                try:
                    cols = [sa_func.count(LogEvent.id).label("total")]
                    for t in templates:
                        cols.append(
                            sa_func.sum(
                                case(
                                    (LogEvent.data.has_key(t.key), literal(1)),
                                    else_=literal(0),
                                ),
                            ).label(f"has_{t.id}"),
                        )
                    row = (
                        session.query(*cols)
                        .join(
                            LogEventContext,
                            LogEventContext.log_event_id == LogEvent.id,
                        )
                        .filter(
                            LogEvent.project_id == project_id,
                            LogEventContext.context_id == context_id,
                        )
                        .one()
                    )
                    total = row[0] or 0
                    for i, t in enumerate(templates):
                        has_count = row[i + 1] or 0
                        pending_by_template[t.id] = total - has_count
                except Exception as e:
                    logging.warning(
                        f"Error in dry_run for non-emb scope "
                        f"({project_id}, {context_id}): {e}",
                    )
                    for t in templates:
                        pending_by_template[t.id] = -1

            # 2) Embedding: check Embedding table per scope
            #    One query per scope gets total logs + per-key embedding counts.
            for (project_id, context_id), templates in scope_emb.items():
                if time.time() - start_time >= max_time_seconds:
                    dry_run_timed_out = True
                    break
                try:
                    total = (
                        session.query(sa_func.count(LogEvent.id))
                        .join(
                            LogEventContext,
                            LogEventContext.log_event_id == LogEvent.id,
                        )
                        .filter(
                            LogEvent.project_id == project_id,
                            LogEventContext.context_id == context_id,
                        )
                        .scalar()
                    ) or 0

                    emb_keys = [t.key for t in templates]
                    emb_counts = dict(
                        session.query(
                            Embedding.key,
                            sa_func.count(sa_func.distinct(Embedding.ref_id)),
                        )
                        .join(LogEvent, LogEvent.id == Embedding.ref_id)
                        .join(
                            LogEventContext,
                            LogEventContext.log_event_id == LogEvent.id,
                        )
                        .filter(
                            LogEvent.project_id == project_id,
                            LogEventContext.context_id == context_id,
                            Embedding.key.in_(emb_keys),
                            Embedding.is_deleted == False,  # noqa: E712
                        )
                        .group_by(Embedding.key)
                        .all(),
                    )

                    for t in templates:
                        has_count = emb_counts.get(t.key, 0)
                        pending_by_template[t.id] = total - has_count
                except Exception as e:
                    logging.warning(
                        f"Error in dry_run for emb scope "
                        f"({project_id}, {context_id}): {e}",
                    )
                    for t in templates:
                        pending_by_template[t.id] = -1

            # Bulk-fetch project and context names for readable output
            project_ids = {t.project_id for t in active_templates}
            context_ids = {t.context_id for t in active_templates}
            project_names = dict(
                session.query(Project.id, Project.name)
                .filter(Project.id.in_(project_ids))
                .all(),
            )
            context_names = dict(
                session.query(Context.id, Context.name)
                .filter(Context.id.in_(context_ids))
                .all(),
            )

            # Build response — only include templates with pending > 0
            templates_with_pending = []
            total_pending = 0
            templates_scanned = len(pending_by_template)
            templates_zero = 0
            templates_error = 0
            for template in active_templates:
                pending = pending_by_template.get(template.id)
                if pending is None:
                    continue
                if pending == -1:
                    templates_error += 1
                elif pending > 0:
                    total_pending += pending
                    templates_with_pending.append(
                        {
                            "id": template.id,
                            "project_id": template.project_id,
                            "project_name": project_names.get(template.project_id),
                            "context_id": template.context_id,
                            "context_name": context_names.get(template.context_id),
                            "key": template.key,
                            "equation": template.equation,
                            "pending_logs": pending,
                            "is_embedding": _is_embedding_template(template),
                            "has_filter_expression": template.filter_expression
                            is not None,
                        },
                    )
                else:
                    templates_zero += 1

            return {
                "dry_run": True,
                "total_templates": len(active_templates),
                "templates_scanned": templates_scanned,
                "templates_remaining": len(active_templates) - templates_scanned,
                "templates_with_pending": len(templates_with_pending),
                "templates_zero": templates_zero,
                "templates_error": templates_error,
                "total_pending_logs": total_pending,
                "timed_out": dry_run_timed_out,
                "elapsed_seconds": round(time.time() - start_time, 2),
                "templates": templates_with_pending,
            }

        # --- Processing mode ---
        derived_log_dao = LogEventDAO(session, context_dao)
        total_logs_processed = 0
        templates_processed = []
        templates_skipped = 0
        timed_out = False
        cap_reached = False

        for template in active_templates:
            elapsed = time.time() - start_time
            if elapsed >= max_time_seconds:
                timed_out = True
                break
            if total_logs_processed >= max_logs:
                cap_reached = True
                break

            try:
                remaining = max_logs - total_logs_processed
                pending_query = _build_pending_query(template)
                matching_log_events = pending_query.limit(remaining).all()
                matching_log_event_ids = [row[0] for row in matching_log_events]

                if not matching_log_event_ids:
                    templates_processed.append(
                        {
                            "id": template.id,
                            "key": template.key,
                            "logs_processed": 0,
                            "logs_matched": 0,
                        },
                    )
                    continue

                count = derived_log_dao.recompute_derived_logs(
                    template=template,
                    log_ids=matching_log_event_ids,
                    json_encoder=CustomEncoder,
                    field_type_dao=field_type_dao,
                )
                total_logs_processed += count
                templates_processed.append(
                    {
                        "id": template.id,
                        "key": template.key,
                        "logs_processed": count,
                        "logs_matched": len(matching_log_event_ids),
                    },
                )

            except Exception as template_error:
                logging.warning(
                    f"Error processing template {template.id}: {template_error}",
                )
                templates_processed.append(
                    {
                        "id": template.id,
                        "key": template.key,
                        "error": str(template_error),
                    },
                )
                continue

        templates_skipped = len(active_templates) - len(templates_processed)

        return {
            "total_templates_processed": len(templates_processed),
            "total_templates_skipped": templates_skipped,
            "total_derived_logs_created": total_logs_processed,
            "timed_out": timed_out,
            "cap_reached": cap_reached,
            "elapsed_seconds": round(time.time() - start_time, 2),
            "templates_processed": templates_processed,
        }

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error processing active derived log templates: {str(e)}",
        )


# =============================================================================
# Decoupled Embedding Pipeline Constants (Stage 1: Generation, Stage 2: Insertion)
# =============================================================================

# Stage 1: Embedding Generation (parallel-safe)
GENERATION_DEFAULT_MAX_ITEMS = 4096  # 2 OpenAI batches of 2048
GENERATION_MAX_ITEMS_HARD_CAP = 8192
GENERATION_DEFAULT_MAX_TIME = 40  # ~15-20s per batch + overhead
GENERATION_MAX_TIME_HARD_CAP = 120

# Stage 2: Index Insertion (serial)
INSERTION_DEFAULT_MAX_ITEMS = 12000  # For 3-minute schedule
INSERTION_MAX_ITEMS_HARD_CAP = 20000
INSERTION_DEFAULT_MAX_TIME = 150  # ~100s at 100/s + buffer
INSERTION_MAX_TIME_HARD_CAP = 300


@admin_router.post(
    "/run_index_maintenance",
    responses={
        200: {
            "description": "Index maintenance completed successfully",
            "content": {
                "application/json": {
                    "example": {
                        "message": "Index maintenance completed in 95.5s. Deleted 3562 embeddings.",
                        "status": "success",
                        "skipped": False,
                        "metrics": {
                            "mode": "auto",
                            "soft_deleted_count": 3562,
                            "total_embeddings": 2058127,
                            "invalid_indexes_found": [],
                            "invalid_indexes_cleaned": [],
                            "deletion_metrics": {
                                "total_deleted": 3562,
                                "batch_count": 1,
                                "duration": 12.5,
                            },
                            "reindex_results": {
                                "embedding_hnsw_cosine_openai_1536_idx": {
                                    "action": "reindexed",
                                    "duration": 75.2,
                                    "success": True,
                                },
                            },
                            "phases_executed": [
                                "invalid_index_cleanup",
                                "batched_delete",
                                "reindex",
                                "vacuum",
                            ],
                            "durations": {
                                "invalid_index_cleanup": 0.1,
                                "batched_delete": 12.5,
                                "reindex": 75.0,
                                "vacuum": 7.9,
                            },
                        },
                    },
                },
            },
        },
        500: {
            "description": "Internal Server Error",
        },
    },
)
def run_index_maintenance(
    mode: str = Query(
        "auto",
        description="Maintenance mode: "
        "'auto' (smart threshold-based, default for nightly runs), "
        "'full' (force all phases, good for weekly), "
        "'cleanup_only' (delete soft-deleted rows, skip reindex), "
        "'reindex_only' (reindex only, skip deletion), "
        "'check' (dry run, report metrics only)",
    ),
    soft_delete_threshold: int = Query(
        100,
        ge=0,
        description="Minimum soft-deleted rows before cleanup runs in 'auto' mode. "
        "Default: 100. Set to 0 to always cleanup if any soft-deleted rows exist.",
    ),
    skip_vacuum: bool = Query(
        False,
        description="Skip VACUUM phase after maintenance. "
        "Faster but doesn't immediately reclaim disk space.",
    ),
    max_duration: int = Query(
        0,
        ge=0,
        description="Maximum duration in seconds. When set, the job will allocate "
        "time budgets to phases and stop gracefully before the deadline. "
        "Set to 0 for unlimited (default). Recommended: 600-900 for cleanup_only, "
        "3600+ for full maintenance. Should be less than Cloud Scheduler attempt deadline.",
    ),
    session=Depends(get_db_session),
    _=Depends(auth_admin_key),
):
    """
    Trigger HNSW index maintenance.

    **Maintenance Modes:**
    - `auto` (default): Smart threshold-based. Cleanup if soft-deleted >= threshold,
      reindex only if cleanup happened. Best for nightly runs.
    - `full`: Run all phases regardless of thresholds. Best for weekly optimization.
    - `cleanup_only`: Only delete soft-deleted rows, skip expensive reindex.
      Good for frequent cleanup without downtime risk.
    - `reindex_only`: Only reindex, skip deletion. Use when index is fragmented
      but no soft deletes exist.
    - `check`: Dry run - just report metrics without making any changes.

    **Recommended Production Setup:**

    Use Cloud Scheduler for short, time-bounded operations only (cleanup, check).
    Use Cloud Run Jobs for long-running operations (reindex) — see below.

    **Cloud Scheduler Jobs (3 jobs, all within 30m deadline):**

    1. **Health Check** (twice daily) - Quick monitoring
       - Cron: `0 8,20 * * *`
       - Params: `mode=check`
       - Attempt deadline: 2m, Max retries: 3

    2. **Cleanup** (every 4 hours) - Frequent soft-delete cleanup
       - Cron: `0 */4 * * *`
       - Params: `mode=cleanup_only&skip_vacuum=true&max_duration=600`
       - Attempt deadline: 15m, Max retries: 2

    3. **Cleanup + Vacuum** (nightly) - Reclaim disk space
       - Cron: `0 3 * * *`
       - Params: `mode=cleanup_only&max_duration=1500`
       - Attempt deadline: 30m, Max retries: 1

    **Cloud Run Job (weekly reindex):**

    REINDEX CONCURRENTLY must NOT run under HTTP timeout pressure.
    An interrupted REINDEX corrupts both old and new indexes, leaving
    them invalid. Use a Cloud Run Job triggered by Cloud Scheduler:

    - Cron: `0 4 * * 0`
    - Container: `python -m orchestra.workers.index_maintenance`
    - Env: `MAINTENANCE_MODE=full`, `MAINTENANCE_SKIP_VACUUM=true`
    - Task timeout: 3h, Max retries: 0

    **Phases (in order):**
    1. Invalid index cleanup (always runs, handles failed CONCURRENTLY operations)
    2. Batched soft-delete cleanup (if mode allows and threshold met)
    3. REINDEX CONCURRENTLY (if mode allows, keeps index usable during rebuild)
    4. VACUUM (if skip_vacuum=false and any work was done)

    **WARNING:** Do not use `mode=auto` or `mode=full` with Cloud Scheduler.
    These modes can trigger REINDEX which exceeds Cloud Scheduler's 30m
    max deadline. Use `mode=cleanup_only` for scheduled HTTP calls and
    Cloud Run Jobs for reindexing.
    """
    try:
        from orchestra.workers.index_maintenance import (
            run_index_maintenance as do_maintenance,
        )

        # Validate mode
        valid_modes = ["auto", "full", "cleanup_only", "reindex_only", "check"]
        if mode not in valid_modes:
            return JSONResponse(
                status_code=400,
                content={
                    "detail": f"Invalid mode '{mode}'. Must be one of: {valid_modes}",
                },
            )

        # Run maintenance with specified mode
        metrics = do_maintenance(
            session=session,
            mode=mode,
            soft_delete_threshold=soft_delete_threshold,
            skip_vacuum=skip_vacuum,
            max_duration_seconds=max_duration,
        )

        if metrics["success"]:
            deleted = metrics.get("deletion_metrics", {}).get("total_deleted", 0)
            total_duration = sum(metrics.get("durations", {}).values())

            if metrics.get("skipped"):
                return {
                    "message": f"Skipped: {metrics['soft_deleted_count']} soft-deleted rows "
                    f"(threshold: {soft_delete_threshold}), "
                    f"{len(metrics.get('invalid_indexes_found', []))} invalid indexes",
                    "status": "success",
                    "skipped": True,
                    "metrics": metrics,
                }

            return {
                "message": f"Index maintenance completed in {total_duration:.1f}s. "
                f"Deleted {deleted} embeddings. Phases: {metrics['phases_executed']}",
                "status": "success",
                "skipped": False,
                "metrics": metrics,
            }
        else:
            return JSONResponse(
                status_code=500,
                content={
                    "detail": f"Index maintenance failed: {metrics.get('error', 'Unknown error')}",
                    "metrics": metrics,
                },
            )

    except Exception as e:
        import logging as _logging

        _logging.getLogger(__name__).exception("Error running index maintenance")
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error"},
        )


# =============================================================================
# Decoupled Embedding Pipeline Endpoints (Stage 1 + Stage 2)
# =============================================================================


@admin_router.post(
    "/generate_pending_embeddings",
    responses={
        200: {
            "description": "Successfully processed pending embeddings",
            "content": {
                "application/json": {
                    "example": {
                        "message": "Generated vectors for 4096 embeddings",
                        "status": "success",
                        "metrics": {
                            "processed": 4096,
                            "successful": 4096,
                            "failed": 0,
                            "stale_reset": 0,
                            "duration_seconds": 18.5,
                            "throughput_per_second": 221.4,
                            "queue_drained": False,
                            "queue_metrics": {
                                "pending": 50000,
                                "generating": 0,
                                "vector_ready": 4096,
                                "inserting": 0,
                                "failed": 0,
                                "cancelled": 0,
                            },
                        },
                    },
                },
            },
        },
        500: {
            "description": "Internal Server Error",
        },
    },
)
def generate_pending_embeddings(
    max_items: int = Query(
        GENERATION_DEFAULT_MAX_ITEMS,
        le=GENERATION_MAX_ITEMS_HARD_CAP,
        description="Maximum items to process. Default: 4096 (2 OpenAI batches), Hard cap: 8192.",
    ),
    max_time_seconds: int = Query(
        GENERATION_DEFAULT_MAX_TIME,
        le=GENERATION_MAX_TIME_HARD_CAP,
        description="Maximum processing time in seconds. Default: 40s, Hard cap: 120s.",
    ),
    retry_failed: bool = Query(
        False,
        description="If true, retry items with status='failed' instead of 'pending'. "
        "Failed items get retry_count reset to 0 and error_message cleared.",
    ),
    dry_run: bool = Query(
        False,
        description="If true, return queue status counts only without processing anything.",
    ),
    session=Depends(get_db_session),
    _=Depends(auth_admin_key),
):
    """
    Stage 1: Generate embedding vectors for pending queue items.

    This endpoint is **SAFE FOR PARALLEL EXECUTION**. Multiple workers can call
    this endpoint simultaneously - FOR UPDATE SKIP LOCKED ensures each queue
    item is processed by exactly one worker.

    **Flow:**
    1. Reset stale 'generating' items back to 'pending' (crash recovery)
    2. Claim batch: pending → generating (atomic with FOR UPDATE SKIP LOCKED)
    3. Generate vectors via OpenAI API (batched by model, 2048 max per API call)
    4. Update queue: status='vector_ready', generated_vector=<vector>
    5. On error: increment retry_count or mark 'failed'

    **Modes:**
    - `retry_failed=false` (default): Process items with status='pending'
    - `retry_failed=true`: Retry items with status='failed', resetting retry_count to 0
    - `dry_run=true`: Report queue status counts only, no processing

    **Cloud Scheduler Configuration (2 parallel jobs for pending):**
    - Job 1: Schedule "0,30 * * * * *" (at :00 and :30 of each minute)
      POST /admin/generate_pending_embeddings?max_items=4096&max_time_seconds=40
    - Job 2: Schedule "15,45 * * * * *" (at :15 and :45 of each minute)
      POST /admin/generate_pending_embeddings?max_items=4096&max_time_seconds=40

    **Cloud Scheduler Configuration (1 job for retry_failed every 30 min):**
    - Schedule: "0,30 * * * *" (at :00 and :30 of each hour)
      POST /admin/generate_pending_embeddings?max_items=2048&max_time_seconds=60&retry_failed=true

    **TODO:** Migrate to Cloud Tasks for dynamic scaling based on queue depth.
    """
    try:
        from orchestra.workers.embedding_generator import (
            get_generation_queue_metrics,
            process_pending_embeddings,
        )

        if dry_run:
            return {
                "message": "Dry run: queue status only",
                "status": "success",
                "dry_run": True,
                "queue_metrics": get_generation_queue_metrics(session),
            }

        # Process embeddings (generate vectors)
        # Uses bulk UPDATE for O(1) database operations instead of O(N)
        result = process_pending_embeddings(
            session,
            max_items=max_items,
            max_time_seconds=max_time_seconds,
            include_metrics=True,  # Include queue status counts in response
            retry_failed=retry_failed,
        )

        successful = result.get("successful", 0)
        failed = result.get("failed", 0)
        mode = result.get("mode", "pending")

        if successful == 0 and failed == 0 and result.get("processed", 0) == 0:
            msg = (
                "No failed embeddings to retry"
                if retry_failed
                else "No pending embeddings to generate"
            )
            return {
                "message": msg,
                "status": "success",
                "queue_drained": True,
                "metrics": result,
            }

        action = "Retried" if retry_failed else "Generated vectors for"
        return {
            "message": f"{action} {successful} embeddings"
            + (f" ({failed} failed)" if failed else ""),
            "status": "success",
            "queue_drained": result.get("queue_drained", False),
            "metrics": result,
        }

    except Exception as e:
        import logging as _logging

        _logging.getLogger(__name__).exception("Error generating embeddings")
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error"},
        )


@admin_router.post(
    "/index_ready_embeddings",
    responses={
        200: {
            "description": "Successfully inserted embeddings into index",
            "content": {
                "application/json": {
                    "example": {
                        "message": "Inserted 12000 embeddings into index",
                        "status": "success",
                        "metrics": {
                            "processed": 12000,
                            "inserted": 12000,
                            "failed": 0,
                            "stale_reset": 0,
                            "duration_seconds": 110.5,
                            "throughput_per_second": 108.6,
                            "queue_drained": False,
                            "queue_metrics": {
                                "pending": 50000,
                                "generating": 4096,
                                "vector_ready": 38000,
                                "inserting": 0,
                                "failed": 0,
                                "cancelled": 0,
                            },
                        },
                    },
                },
            },
        },
        500: {
            "description": "Internal Server Error",
        },
    },
)
def index_ready_embeddings(
    max_items: int = Query(
        INSERTION_DEFAULT_MAX_ITEMS,
        le=INSERTION_MAX_ITEMS_HARD_CAP,
        description="Maximum items to insert. Default: 12000, Hard cap: 20000.",
    ),
    max_time_seconds: int = Query(
        INSERTION_DEFAULT_MAX_TIME,
        le=INSERTION_MAX_TIME_HARD_CAP,
        description="Maximum processing time in seconds. Default: 150s, Hard cap: 300s.",
    ),
    dry_run: bool = Query(
        False,
        description="If true, return queue status counts only without processing anything.",
    ),
    session=Depends(get_db_session),
    _=Depends(auth_admin_key),
):
    """
    Stage 2: Bulk insert generated embeddings into the indexed Embedding table.

    This endpoint should run **SERIALLY** (one worker at a time) for optimal
    HNSW index performance. While technically safe with FOR UPDATE SKIP LOCKED,
    parallel insertion degrades performance due to index lock contention.

    **Flow:**
    1. Reset stale 'inserting' items back to 'vector_ready' (crash recovery)
    2. Claim batch: vector_ready → inserting (atomic with FOR UPDATE SKIP LOCKED)
    3. Bulk INSERT into Embedding table (ON CONFLICT DO UPDATE for soft-delete resurrection)
    4. DELETE successfully inserted items from queue
    5. On error: mark items as 'failed' with error message

    **Modes:**
    - `dry_run=true`: Report queue status counts only, no processing

    **Cloud Scheduler Configuration (1 serial job):**
    - Schedule: "*/3 * * * *" (every 3 minutes)
    - URL: POST /admin/index_ready_embeddings?max_items=12000&max_time_seconds=150
    - Attempt deadline: 180s

    **TODO:** Migrate to Cloud Tasks for dynamic scaling. With Cloud Tasks,
    a single task can handle larger batches with longer timeouts, and tasks
    can be dispatched based on queue depth.
    """
    try:
        from orchestra.workers.embedding_inserter import (
            get_insertion_queue_metrics,
            process_ready_embeddings,
        )

        if dry_run:
            return {
                "message": "Dry run: queue status only",
                "status": "success",
                "dry_run": True,
                "queue_metrics": get_insertion_queue_metrics(session),
            }

        # Process embeddings (bulk insert into index)
        # Uses dynamic chunk sizing based on max_items
        result = process_ready_embeddings(
            session,
            max_items=max_items,
            max_time_seconds=max_time_seconds,
            include_metrics=True,  # Include queue status counts in response
        )

        inserted = result.get("inserted", 0)
        failed = result.get("failed", 0)

        if inserted == 0 and failed == 0 and result.get("processed", 0) == 0:
            return {
                "message": "No ready embeddings to insert",
                "status": "success",
                "queue_drained": True,
                "metrics": result,
            }

        return {
            "message": f"Inserted {inserted} embeddings into index"
            + (f" ({failed} failed)" if failed else ""),
            "status": "success",
            "metrics": result,
        }

    except Exception as e:
        import logging as _logging

        _logging.getLogger(__name__).exception("Error inserting embeddings")
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error"},
        )
