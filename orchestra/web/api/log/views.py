"""

Includes endpoints related to entries.
"""

import json
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple, Union

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from sqlalchemy import (
    INTEGER,
    TIMESTAMP,
    DateTime,
    Float,
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
)
from sqlalchemy.dialects.postgresql import BOOLEAN, JSONB
from sqlalchemy.orm import aliased
from sqlalchemy.sql.selectable import Subquery

from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dao.derived_log_dao import DerivedLogDAO
from orchestra.db.dao.field_type_dao import FieldTypeDAO
from orchestra.db.dao.log_dao import LogDAO, OverwriteError
from orchestra.db.dao.log_event_dao import LogEventDAO
from orchestra.db.dao.project_dao import ProjectDAO
from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import (
    DerivedLog,
    Log,
    LogEvent,
    LogEventContext,
)
from orchestra.web.api.log.schema import (
    CreateDerivedEntriesConfig,
    CreateLogConfig,
    DeleteLogEntryRequest,
    SetFieldTypingRequest,
    UpdateDerivedEntriesConfig,
    UpdateLogRequest,
)
from orchestra.web.api.utils.http_responses import not_found

from .helpers import (
    STR_TO_SQL_TYPES,
    _compute_expression,
    _flatten_fields,
    _format_flat_logs,
    _get_final_logs,
    _substitute_placeholders,
    build_sql_query,
    str_filter_exp_to_dict,
)

router = APIRouter()


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
    A "explicit_types" dictionary can be passed as part of the `entries`.
    If present, any matching key inside this dictionary will override the
    inferred type of that particular entry.

    This method returns the ids of the new stored logs.
    """
    # check if the project exists
    try:
        # TODO: Add organization id
        user_id = request_fastapi.state.user_id
        project = project_dao.filter(user_id=user_id, name=request.project)
        project_id = project[0][0].id
    except IndexError:
        raise not_found("Project")

    # Convert single entries/params to list format for uniform processing
    entries_list = (
        request.entries if isinstance(request.entries, list) else [request.entries]
    )
    params_list = (
        request.params if isinstance(request.params, list) else [request.params]
    )

    # Validate that params and entries lists have equal lengths when both are provided as lists
    if isinstance(request.entries, list) and isinstance(request.params, list):
        if len(request.entries) != len(request.params):
            raise HTTPException(
                status_code=400,
                detail=f"When both 'params' and 'entries' are provided as lists, they must have equal lengths. "
                f"Got params length: {len(request.params)}, entries length: {len(request.entries)}",
            )
    elif isinstance(request.entries, list) and not isinstance(request.params, list):
        raise HTTPException(
            status_code=400,
            detail="If 'entries' is a list, 'params' must also be a list or None.",
        )
    elif not isinstance(request.entries, list) and isinstance(request.params, list):
        raise HTTPException(
            status_code=400,
            detail="If 'params' is a list, 'entries' must also be a list or None.",
        )

    # Get field types once for all operations
    field_types = field_type_dao.get_field_types(project_id)

    def enforce_types(field_name, value, batch_index=None):
        entered_type = LogDAO.infer_type(field_name, value)
        expected_type = field_types.get(field_name)
        if expected_type:
            if expected_type == "NoneType":
                if entered_type == "NoneType":
                    return
                # update the field type to the new type
                field_type_dao.upsert_field_type(project_id, field_name, value)
            elif entered_type != expected_type:
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
            field_type_dao.create_field_type_if_absent(project_id, field_name, value)

    def get_context_id():
        if request.context:
            return context_dao.get_or_create(
                project_id=project_id,
                name=request.context.name,
                description=request.context.description,
            )
        else:
            return None

    # Process each log in the batch
    log_event_ids = []
    entries_len = len(entries_list)
    params_len = len(params_list)

    total_logs = max(entries_len, params_len)

    for i in range(total_logs):
        # Get or create context_id
        context_id = get_context_id()

        # Create log_event for each log
        log_event_id = log_event_dao.create(
            project_id=project_id,
            context_id=context_id,
        )
        log_event_ids.append(log_event_id)

        # Get current entries and params
        # If i exceeds list length, use the last item in the list
        current_entries = entries_list[min(i, entries_len - 1)]
        current_params = params_list[min(i, params_len - 1)]

        # Extract explicit types
        entries_explicit_types = (
            current_entries.pop("explicit_types", None)
            if isinstance(current_entries, dict)
            else None
        )
        params_explicit_types = (
            current_params.pop("explicit_types", None)
            if isinstance(current_params, dict)
            else None
        )
        # Process params
        for k, v in current_params.items():
            enforce_types(k, v, i)
            # see if there is any param with the same value
            existing_param = log_dao.filter(
                key=k,
                value=json.dumps(v),
                project_id=project_id,
            )
            if existing_param:
                version = existing_param[0][0].version
            else:
                # fetch the highest version for that param
                existing_params = log_dao.filter(key=k, project_id=project_id)
                highest_version = max([-1] + [e[0].version for e in existing_params])
                version = highest_version + 1
            try:
                log_dao.create_from_raw_k_v(
                    project_id=project_id,
                    log_event_id=log_event_id,
                    raw_k=k,
                    raw_v=v,
                    version=version,
                    explicit_types=params_explicit_types,
                )
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail="Found different value for log params with same version.",
                )

        # Process entries
        for k, v in current_entries.items():
            enforce_types(k, v, i)
            log_dao.create_from_raw_k_v(
                project_id=project_id,
                log_event_id=log_event_id,
                raw_k=k,
                raw_v=v,
                explicit_types=entries_explicit_types,
            )

    return log_event_ids


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
    """
    user_id = request_fastapi.state.user_id

    # 1) Validate the project
    try:
        project_obj = project_dao.filter(name=body.project, user_id=user_id)[0][0]
    except IndexError:
        raise HTTPException(
            status_code=404,
            detail=f"Project '{body.project}' not found.",
        )

    # 3) Resolve referenced_logs
    #    We either get a direct list [101,102], or a dict e.g. {"filter_expr":...}
    resolved_ids: Dict[str, List[int]] = {}
    for varname, val in body.referenced_logs.items():
        if isinstance(val, list):
            resolved_ids[varname] = val
        elif isinstance(val, dict):
            # Re-use _get_logs_query to find matching log_event_ids
            # raw_rows is a list of:
            # - row_key
            # - row_value
            # - row_inferred_type
            # - row_version
            # - row_source_type
            # - row_created_at
            # - row_event_id
            raw_rows, _, _count = _get_logs_query(
                request_fastapi=request_fastapi,
                project=body.project,
                column_context=val.get("column_context", None),
                context=val.get("context", None),
                filter_expr=val.get("filter_expr", None),
                sorting=val.get("sort"),
                from_ids=val.get("from_ids", None),
                exclude_ids=val.get("exclude_ids", None),
                from_fields=val.get("from_fields", None),
                exclude_fields=val.get("exclude_fields", None),
                limit=val.get("limit"),
                offset=val.get("offset", 0),
                project_dao=project_dao,
                field_type_dao=field_type_dao,
                context_dao=context_dao,
                session=session,
            )
            # Extract log_event_ids from raw rows
            le_ids = list({r[6] for r in raw_rows})  # r[6] is log_event_id
            resolved_ids[varname] = le_ids
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Unrecognized reference for '{varname}': {val}",
            )

    # If we want a 1:1 mapping, ensure all reference arrays have the same length
    lengths = [len(lst) for lst in resolved_ids.values()]
    if not lengths:
        return {"info": "No references found. Nothing to create."}
    if len(set(lengths)) != 1:
        raise HTTPException(
            status_code=400,
            detail=f"All referenced log lists must have the same length. Found lengths: {lengths}",
        )

    created_derived_ids = []
    try:

        # 5) Build a filter_dict that references those base logs. Then compute
        filter_expr, alias_to_key_map = _substitute_placeholders(
            body.equation,
            resolved_ids,
        )
        filter_dict = str_filter_exp_to_dict(filter_expr)
        computed_values = _compute_expression(filter_dict, LogEvent, session)

        # Create a new derived log entry for each computed value
        class DecimalEncoder(json.JSONEncoder):
            def default(self, obj):
                if isinstance(obj, Decimal):
                    return float(obj)
                return super().default(obj)

        # Iterate over the computed values and resolved IDs
        for i, (_, value) in enumerate(computed_values):
            # Create a dictionary for the current set of referenced logs
            current_referenced_logs = {
                alias_to_key_map[key]: ids[i] for key, ids in resolved_ids.items()
            }
            # Get all log IDs involved in this specific computation
            involved_log_ids = [ids[i] for ids in resolved_ids.values()]

            # Create a derived entry for each log ID involved in this computation
            for log_event_id in involved_log_ids:
                val = json.loads(json.dumps(value, cls=DecimalEncoder))
                inferred_type = LogDAO.infer_type("", val)
                new_derived_id = derived_log_dao.create(
                    log_event_id=log_event_id,
                    key=body.key,
                    equation=body.equation,
                    referenced_logs=current_referenced_logs,
                    value=val,
                    inferred_type=inferred_type,
                )
                created_derived_ids.append(new_derived_id)

        # Create a field type for the derived log
        field_type_dao.create_field_type_if_absent(project_obj.id, body.key, val)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create derived logs with key='{body.key}'. Error: {e}",
        )
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
    request: UpdateDerivedEntriesConfig,
    derived_log_dao: DerivedLogDAO = Depends(),
    log_event_dao: LogEventDAO = Depends(),
    session=Depends(get_db_session),
):
    """Updates multiple derived log entries with new key, equation, or referenced logs.
    Handles batch updates and recomputes values as needed."""
    try:
        not_found_logs = []
        updated_logs = []

        for derived_log_id in request.ids:
            try:
                # Check if user has permission to update this derived log
                log_event_id = (
                    derived_log_dao.session.query(DerivedLog.log_event_id)
                    .filter(DerivedLog.id == derived_log_id)
                    .scalar()
                )
                if (
                    not log_event_id
                    or log_event_dao.get_user_id(id=log_event_id)
                    != request_fastapi.state.user_id
                ):
                    not_found_logs.append(derived_log_id)
                    continue

                # Update the derived log
                updated_log = derived_log_dao.update(
                    id=derived_log_id,
                    original_key=request.original_key,
                    key=request.key,
                    equation=request.equation,
                )
                updated_logs.append(updated_log)

            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))

        if not_found_logs:
            raise HTTPException(
                status_code=404,
                detail=f"Derived logs with ids {not_found_logs} not found or you don't have permission to update them.",
            )

        # Recompute values for all updated logs
        if updated_logs:
            derived_log_dao.recompute_derived_logs(updated_logs, session)

        return {"info": "Derived logs updated successfully!"}

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


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
    log_dao: LogDAO = Depends(),
    log_event_dao: LogEventDAO = Depends(),
    field_type_dao: FieldTypeDAO = Depends(),
    derived_log_dao: DerivedLogDAO = Depends(),
    session=Depends(get_db_session),
):
    """
    Updates multiple logs with the provided entries. Each entry will be either added
    or overridden in the specified logs.

    A dictionary of "explicit_types" can be passed as part of the `entries`.
    If present, it will override the inferred type of any matching key in all logs.
    """
    updated_ids = set()
    for data_type in ("params", "entries"):

        data = getattr(body, data_type)
        not_found_logs = []

        for i, log_id in enumerate(body.ids):

            try:
                # Get user and project ID for the log
                project_user_id, project_id = log_event_dao.get_user_and_project_id(
                    id=log_id,
                )

                # Check if the log belongs to the requesting user
                if project_user_id != request_fastapi.state.user_id:
                    raise IndexError

            except IndexError:
                not_found_logs.append(log_id)
                continue

            try:
                this_data = data if isinstance(data, dict) else data[i]
            except IndexError:
                raise HTTPException(
                    status_code=400,
                    detail=f"entries and params must be of the same length as log ids ({len(body.ids)}) if passed as a list, but found {data_type} list of length {len(data)}",
                )

            explicit_types = this_data.pop("explicit_types", None)
            field_types = field_type_dao.get_field_types(project_id)
            for k, v in this_data.items():

                if k in field_types:
                    expected_type = field_types[k]
                    original_type = LogDAO.infer_type(k, v)
                    if expected_type == "NoneType":
                        field_type_dao.upsert_field_type(project_id, k, v)
                    elif original_type != expected_type and original_type != "NoneType":
                        raise HTTPException(
                            status_code=400,
                            detail=f"Type mismatch for field '{k}': expected {expected_type}, got {original_type}",
                        )
                else:
                    field_type_dao.create_field_type_if_absent(project_id, k, v)

                # see if there is any param with the same value
                existing = log_dao.filter(
                    key=k,
                    value=json.dumps(v),
                    project_id=project_id,
                )
                if data_type == "params":
                    if existing:
                        version = existing[0][0].version
                    else:
                        # fetch the highest version for that param
                        existing_params = log_dao.filter(key=k, project_id=project_id)
                        highest_version = max(
                            [-1] + [e[0].version for e in existing_params],
                        )
                        version = highest_version + 1
                elif data_type == "entries":
                    version = None
                else:
                    raise HTTPException(
                        status_code=400,
                        detail="data_type must either be 'params' or 'entries', "
                        f"but found {data_type}",
                    )
                try:
                    log_dao.update_value(
                        log_event_id=log_id,
                        raw_k=k,
                        raw_v=v,
                        version=version,
                        explicit_types=explicit_types,
                        overwrite=body.overwrite,
                    )
                    updated_ids.add((k, log_id))
                except IndexError:
                    log_dao.create_from_raw_k_v(
                        project_id=project_id,
                        log_event_id=log_id,
                        raw_k=k,
                        raw_v=v,
                        version=version,
                        explicit_types=explicit_types,
                    )
                except ValueError:
                    raise HTTPException(
                        status_code=400,
                        detail="Found different value for log params with same version.",
                    )
                except OverwriteError:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Found existing value for log entry with key {k} but overwrite is set to False.",
                    )

        if not_found_logs:
            raise HTTPException(
                status_code=404,
                detail=f"Logs with ids {not_found_logs} not found or you don't have permission to update them.",
            )
    # Now recompute the derived logs that reference any updated base logs
    # We'll find derived logs that have `referenced_logs` containing *any* of these updated_log_ids
    if updated_ids:
        # Convert updated_ids to a list of JSONB objects for containment check
        updated_ids_jsonb = [f'{{"{key}": {value}}}' for (key, value) in updated_ids]

        # Find derived logs that need to be recomputed
        derived_logs_to_recompute = (
            session.query(DerivedLog)
            .join(LogEvent, LogEvent.id == DerivedLog.log_event_id)
            .filter(LogEvent.project_id == project_id)
            .filter(
                or_(
                    *[
                        DerivedLog.referenced_logs.op("@>")(jsonb_obj)
                        for jsonb_obj in updated_ids_jsonb
                    ],
                ),
            )
            .all()
        )

        if derived_logs_to_recompute:
            derived_log_dao.recompute_derived_logs(derived_logs_to_recompute, session)

    return {"info": "Logs updated successfully!"}


@router.delete(
    "/logs",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {
                        "info": "Log entry deleted successfully from all logs!",
                    },
                },
            },
        },
        404_1: {
            "description": "Log Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "One or more logs with the specified IDs were not found.",
                    },
                },
            },
        },
        404_2: {
            "description": "Log Entry Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Log entry <entry> not found in one or more logs.",
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
    log_dao: LogDAO = Depends(),
):
    """
    Deletes a specific entry from multiple logs.
    """

    not_found_logs = []
    not_found_entries = []

    ids_and_fields = _flatten_fields(body.ids_and_fields)

    for log_id, fields in ids_and_fields.items():
        # Verify if the log belongs to the user
        try:
            if log_event_dao.get_user_id(id=log_id) != request_fastapi.state.user_id:
                raise IndexError
        except IndexError:
            not_found_logs.append(log_id)
            continue

        if len(fields) == 0:
            log_event_dao.delete(log_id)
        else:
            for field in fields:
                # Check for the existence of the log entry
                log = log_dao.filter(log_event_id=log_id, key=field)
                if not log:
                    not_found_entries.append(log_id)
                    continue

                # Delete the log entry
                log_dao.delete(id=log[0][0].id)

        if delete_empty_logs and not log_dao.filter(log_event_id=log_id):
            log_event_dao.delete(id=log_id)

    # Handle cases where some logs or entries were not found
    if not_found_logs:
        raise HTTPException(
            status_code=404,
            detail=f"Logs with ids {not_found_logs} not found or you don't have permission to delete from them.",
        )

    if not_found_entries:
        raise HTTPException(
            status_code=404,
            detail=f"Specified fields not found in logs with ids {not_found_entries}.",
        )

    return {"info": "Logs and fields deleted successfully!"}


def _get_logs_query(
    request_fastapi: Request,
    project: str,
    column_context: Optional[str],
    context: Optional[str],
    filter_expr: Optional[str],
    sorting: Optional[str],
    from_ids: Optional[str],
    exclude_ids: Optional[str],
    from_fields: Optional[str],
    exclude_fields: Optional[str],
    limit: Optional[int],
    offset: int,
    project_dao: ProjectDAO,
    field_type_dao: FieldTypeDAO,
    context_dao: ContextDAO,
    session=Depends(get_db_session),
    latest_timestamp=False,
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

    Returns:
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
        project_obj = project_dao.filter(name=project, user_id=user_id)[0][0]
    except IndexError:
        raise not_found(f"Project {project}")
    project_id = project_obj.id

    # 2) Build initial query for relevant LogEvent rows
    #    (filter_expr, from_ids, exclude_ids, plus optional static context)
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

    # Handle user-defined filter_expr => build SQL expression on LogEvent
    if filter_expr:
        filter_dict = str_filter_exp_to_dict(filter_expr)
        if filter_dict:
            condition = build_sql_query(filter_dict, LogEvent, session)
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
        # See if the user-specified context name exists for this project
        context_id = context_dao.filter(name=context, project_id=project_id)
        if context_id:
            ctx_id_val = context_id[0][0].id
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
    base_logs_q = (
        session.query(
            Log.id.label("id"),
            Log.log_event_id.label("log_event_id"),
            Log.key.label("key"),
            Log.value.label("value"),
            Log.inferred_type.label("inferred_type"),
            Log.version.label("version"),
            Log.updated_at.label("updated_at"),
            LogEvent.created_at.label("created_at"),
            literal("base").label("source_type"),
        )
        .join(LogEvent, LogEvent.id == Log.log_event_id)
        .join(relevant_log_events, relevant_log_events.c.id == LogEvent.id)
    )

    derived_logs_q = (
        session.query(
            DerivedLog.id.label("id"),
            DerivedLog.log_event_id.label("log_event_id"),
            DerivedLog.key.label("key"),
            DerivedLog.value.label("value"),
            DerivedLog.inferred_type.label("inferred_type"),
            # derived logs have no version => cast to None
            cast(None, Integer).label("version"),
            DerivedLog.updated_at.label("updated_at"),
            LogEvent.created_at.label("created_at"),
            literal("derived").label("source_type"),
        )
        .join(LogEvent, LogEvent.id == DerivedLog.log_event_id)
        .join(relevant_log_events, relevant_log_events.c.id == LogEvent.id)
    )

    unified_logs_subq = base_logs_q.union_all(derived_logs_q).subquery(
        name="unified_logs",
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

    # If exclude_params / exclude_entries => filter on version
    if exclude_params:
        filtered_logs_q = filtered_logs_q.filter(unified_logs_subq.c.version.is_(None))
    elif exclude_entries:
        filtered_logs_q = filtered_logs_q.filter(
            unified_logs_subq.c.version.isnot(None),
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
    field_types = field_type_dao.get_field_types(project_id)

    if sorting:
        # e.g. sorting='{"score":"ascending","timestamp":"descending"}'
        sort_dict = json.loads(sorting)

        # For each field in sort_dict, we outer-join a subquery from filtered_logs_subq
        # that picks out the relevant value for that field. Then we cast it if known.
        for sort_key, mode in sort_dict.items():
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
                    if cast_type == DateTime
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
    #       id, log_event_id, key, value, inferred_type, version,
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
        row_version,
        row_created_at,
        row_source_type,
    ) in raw_rows:
        results.append(
            (
                row_key,
                row_value,
                row_inferred_type,
                row_version,
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
        description="The context (prepending '/' seperated field names) from which to "
        "retrieve the logs.",
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
    return_ids_only: bool = False,
    project_dao: ProjectDAO = Depends(),
    field_type_dao: FieldTypeDAO = Depends(),
    context_dao: ContextDAO = Depends(),
    session=Depends(get_db_session),
):
    """
    Returns a list of filtered entries from a project. When group_threshold is set,
    entries that appear in at least that many logs will be grouped together in the
    grouped_entries field to reduce response size. When value_limit is set, fields
    that exceed this limit will be clipped and the clipped_fields field will be
    populated.

    The response will include:
    - logs: List of log entries with their individual values
    - params: Dictionary of parameter versions
    - count: Total number of logs
    - grouped_entries: (When group_threshold is set) Dictionary of field names to their shared values
    - clipped_fields: List of fields that were clipped due to value_limit
    """

    # 1) If not grouping, just do the existing (monolithic) approach
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
        )
        if return_ids_only:
            event_ids = [r[6] for r in all_rows]  # (obj, created_at, event_id)
            return list(dict.fromkeys(event_ids))

        # Format
        field_order_map = field_type_dao.get_ordered_field_names(
            project_dao.filter(name=project, user_id=request_fastapi.state.user_id)[0][
                0
            ].id,
        )
        logs_out, params_out = _format_flat_logs(
            all_rows,
            context_len,
            value_limit,
            field_order_map,
        )

        # If group_threshold => factor out repeated fields
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

    # --- GROUPING CASE ---
    # If grouping, do the 2-step approach:
    #    (a) retrieve all event IDs, ignoring limit/offset
    event_ids, total_count = _get_all_filtered_log_event_ids(
        request_fastapi=request_fastapi,
        project=project,
        context=context,
        filter_expr=filter_expr,
        from_ids=from_ids,
        exclude_ids=exclude_ids,
        project_dao=project_dao,
        context_dao=context_dao,
        session=session,
    )
    field_order_map = field_type_dao.get_ordered_field_names(
        project_dao.filter(name=project, user_id=request_fastapi.state.user_id)[0][
            0
        ].id,
    )
    if return_ids_only:
        return list(dict.fromkeys(event_ids))

    # (b) Gather all param versions
    params_out = _get_params_for_log_events(event_ids, session)

    if nested_groups:
        # (c) Build the nested structure
        grouped_result = _build_grouped_data(
            request_fastapi=request_fastapi,
            project=project,
            log_event_ids=event_ids,
            field_order_map=field_order_map,
            group_by=group_by,
            group_depth=group_depth,
            group_limit=group_limit,
            group_offset=group_offset,
            level=0,
            limit=limit,
            offset=offset,
            column_context=column_context,
            from_fields=from_fields,
            exclude_fields=exclude_fields,
            sorting=sorting,
            project_dao=project_dao,
            field_type_dao=field_type_dao,
            context_dao=context_dao,
            session=session,
            value_limit=value_limit,
        )

        return {
            "params": params_out,
            "logs": grouped_result,
            "count": total_count,
        }

    else:
        # use the "flat" groups mode for the view pane.
        # (a) Get the flat logs (without any recursive grouping)
        rows, context_len, _ = _fetch_logs_for_event_ids(
            request_fastapi=request_fastapi,
            event_ids=event_ids,
            column_context=column_context,
            from_fields=from_fields,
            exclude_fields=exclude_fields,
            sorting=sorting,
            limit=limit,
            offset=offset,
            parent_fields="",  # no parent grouping in flat mode
            project_dao=project_dao,
            field_type_dao=field_type_dao,
            session=session,
        )
        logs_out, _ = _format_flat_logs(rows, context_len, value_limit, field_order_map)

        # (b) Build a per-field grouping structure.
        groups = {}

        # A helper to “parse” a group key (e.g. "entries/i" => ("entries", "i"))
        def parse_group_key(key: str) -> Tuple[str, str]:
            parts = key.split("/", 1)
            return (parts[0], parts[1]) if len(parts) == 2 else ("", key)

        for group_field in group_by:
            prefix, raw_key = parse_group_key(group_field)
            is_param = prefix == "params"
            # Retrieve distinct group values (if a log has no such key, it will be added later as "null")
            distinct_values = _get_distinct_group_values(
                log_event_ids=event_ids,
                group_key=raw_key,
                session=session,
                is_param=is_param,
            )
            value_to_ids = {}
            used_ids = set()
            for val in distinct_values:
                subset_ids = _get_log_event_ids_for_group_value(
                    log_event_ids=event_ids,
                    group_key=raw_key,
                    group_value=val,
                    session=session,
                    is_param=is_param,
                )
                value_to_ids[val] = subset_ids
                used_ids.update(subset_ids)
            # Also include a group for logs that do not have the key
            missing_ids = list(set(event_ids) - used_ids)
            if missing_ids:
                value_to_ids["null"] = missing_ids

            # Apply pagination (group_offset / group_limit) to the keys.
            all_keys = list(value_to_ids.keys())
            total_distinct = len(all_keys)
            # (You might want to sort keys in a deterministic way.)
            all_keys_sorted = sorted(all_keys, key=lambda x: (x is None, x))
            if group_limit is not None:
                paged_keys = all_keys_sorted[group_offset : group_offset + group_limit]
            else:
                paged_keys = all_keys_sorted
            paged_mapping = {k: value_to_ids[k] for k in paged_keys}
            # For the count, we return the total number of logs that have this field (or you may sum the lengths)
            field_total = sum(len(ids) for ids in value_to_ids.values())
            groups[group_field] = {
                **paged_mapping,
                "group_count": total_distinct,
                "count": field_total,
            }

        return {
            "params": params_out,
            "groups": groups,
            "logs": logs_out,
            "count": total_count,
        }


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


@router.get(
    "/logs/metric/{metric}",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": 4.56,
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
def get_logs_metric(
    request_fastapi: Request,
    metric: str = Path(
        description="The name of the metric you would like to compute.",
        example="mean",
    ),
    key: str = Query(
        description="The key you would like to extract the reduction metric for.",
        example="score",
    ),
    project: str = Query(
        description="Name of the project to get entries from.",
        example="eval-project",
    ),
    filter_expr: Optional[str] = Query(
        None,
        description="Boolean string to filter entries. TODO: Detailed page.",
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
    session=Depends(get_db_session),
) -> Union[float, int, bool, str]:
    """
    Returns the reduction metric for filtered values (base + derived) for a specific key from a project.
    """
    try:
        user_id = request_fastapi.state.user_id
        project_obj = project_dao.filter(name=project, user_id=user_id)[0][0]
    except IndexError:
        raise not_found(f"Project {project}")
    # TODO: Deal with organisation IDs

    # 1) Build initial query to find matching LogEvent IDs
    #    (i.e. those that pass filter_expr, from_ids, exclude_ids).
    query = session.query(LogEvent.id).filter(LogEvent.project_id == project_obj.id)

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
        filter_dict = str_filter_exp_to_dict(filter_expr)
        if filter_dict:
            condition = build_sql_query(filter_dict, LogEvent, session)
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
    #    We'll unify them into a single subquery that yields (log_event_id, value, inferred_type).
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

    # 3) Now we have a subquery for (log_event_id, value, inferred_type).
    #    We only keep those whose log_event_id is in the `subquery` of filter_expr / from_ids / exclude_ids.
    #    Then we apply the final aggregator (sum, mean, etc.).
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
        (X.c.inferred_type == "float", X.c.value.cast(Float)),
        (X.c.inferred_type == "int", X.c.value.cast(Float)),
        else_=literal(0, type_=Float),
    ).label("value_as_float")

    # Filter the subquery by the log_event_ids that survived above filters
    # (subquery).
    metric_query = (
        session.query(
            reduction_methods[metric](cast_expr),
        )
        .select_from(X)
        .filter(X.c.log_event_id.in_(select(subquery)))
    )

    reduced_query = metric_query.scalar()

    # Post-process based on field type
    field_type = field_type_dao.get_field_types(project_obj.id).get(key)
    if metric == "count":
        return int(reduced_query or 0)

    if reduced_query is None:
        return None

    if not field_type:
        return reduced_query

    # Now do the same final conversions based on the field type:
    if field_type == "timestamp":
        if metric in ("var", "std"):
            return timedelta(seconds=reduced_query).__repr__()
        return datetime.fromtimestamp(reduced_query).isoformat()

    if (
        float(reduced_query).is_integer()
        and metric in ("sum", "min", "max", "median", "mode")
        and field_type in ("int", "bool", "str")
    ):
        if field_type == "bool" and metric in ("min", "max", "median", "mode"):
            return bool(int(reduced_query))
        return int(reduced_query)
    return reduced_query


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
            groups[version] = set()
        groups[version].add(value)
    assert all(
        len(v) == 1 for v in groups.values()
    ), "All sets should contain a single unique value"
    return {k: next(iter(v)) for k, v in groups.items()}


@router.get(
    "/logs/fields",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {
                        "field1": "string",
                        "field2": "int",
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
    project_dao: ProjectDAO = Depends(),
    field_type_dao: FieldTypeDAO = Depends(),
    context_dao: ContextDAO = Depends(),
    session=Depends(get_db_session),
):
    """
    Returns a dictionary of field names and their types for the specified project.
    Strongly typed fields return their type, while others return None.
    """
    try:
        user_id = request_fastapi.state.user_id
        project_obj = project_dao.filter(name=project, user_id=user_id)[0][0]
    except IndexError:
        raise not_found(f"Project {project}")

    types = field_type_dao.get_field_types(project_obj.id)
    # Get all field names from base and derived logs
    log_keys_query = (
        session.query(Log.key)
        .join(LogEvent, LogEvent.id == Log.log_event_id)
        .filter(LogEvent.project_id == project_obj.id)
        .distinct()
    )

    derived_log_keys_query = (
        session.query(DerivedLog.key)
        .join(LogEvent, LogEvent.id == DerivedLog.log_event_id)
        .filter(LogEvent.project_id == project_obj.id)
        .distinct()
    )

    query = log_keys_query.union(derived_log_keys_query)
    all_field_names = "&".join([field.key for field in query.all()])

    # Get raw rows from _get_logs_query
    # raw_rows is a list of:
    # - row_key
    # - row_value
    # - row_inferred_type
    # - row_version
    # - row_source_type
    # - row_created_at
    # - row_event_id
    (raw_rows, _, _) = _get_logs_query(
        request_fastapi,
        project=project,
        column_context=None,
        context=None,
        filter_expr=None,
        sorting=None,
        from_ids=None,
        exclude_ids=None,
        from_fields=all_field_names,
        exclude_fields=None,
        limit=1,
        offset=0,
        project_dao=project_dao,
        field_type_dao=field_type_dao,
        context_dao=context_dao,
        session=session,
        latest_timestamp=False,
    )

    # Process raw rows to determine field types
    field_types = dict(
        (
            row[0],  # key
            (
                "derived_entry"
                if row[4] == "derived"  # source_type
                else "entry"
                if row[3] is None  # version
                else "param"
            ),
        )
        for row in raw_rows
    )

    # Return field types in the same order as they were created
    return {
        key: {
            "data_type": types.get(key),
            "field_type": field_types.get(key),
            "artifacts": (
                session.query(DerivedLog.equation)
                .join(LogEvent, LogEvent.id == DerivedLog.log_event_id)
                .filter(
                    and_(
                        LogEvent.project_id == project_obj.id,
                        DerivedLog.key == key,
                    ),
                )
                .first()[0]
                or ""
            )
            if field_types.get(key) == "derived_entry"
            else "",
        }
        for key in types.keys()
    }


# TODO: this endpoint will become deprecated once we enforce strong typing on all fields.
@router.post(
    "/logs/fields/types",
    responses={
        200: {
            "description": "Field typing updated successfully.",
            "content": {
                "application/json": {
                    "example": {
                        "info": "Field typing updated successfully!",
                    },
                },
            },
        },
        400: {
            "description": "Bad Request - Type mismatch or other validation errors.",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Cannot enable typing for field '<field_name>' as existing logs have different types.",
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
def set_field_types(
    request_fastapi: Request,
    request: SetFieldTypingRequest,
    project: str = Query(
        description="Name of the project to get field types for.",
        example="eval-project",
    ),
    project_dao: ProjectDAO = Depends(),
    field_type_dao: FieldTypeDAO = Depends(),
    log_dao: LogDAO = Depends(),
):
    """
    Sets the typing for specified fields in the project.
    """
    try:
        user_id = request_fastapi.state.user_id
        project_id = project_dao.filter(name=project, user_id=user_id)[0][0].id
    except IndexError:
        raise not_found(f"Project {project}")

    # Check existing logs for each field
    for field_name, should_type in request.types.items():
        if should_type:  # If we want to turn typing on
            existing_logs = log_dao.filter(
                key=field_name,
            )

            # Check if all existing logs for this field are of the same type
            existing_types = {type(log[0].value) for log in existing_logs}
            if len(existing_types) > 1:
                raise HTTPException(
                    status_code=400,
                    detail=f"Cannot enable typing for field '{field_name}' as existing logs have different types.",
                )

            # If all existing logs are of the same type, set the field type
            existing_field_types = field_type_dao.get_field_types(project_id)
            if field_name in existing_field_types:
                # Update the field type if it exists
                field_type_dao.upsert_field_type(
                    project_id,
                    field_name,
                    existing_logs[0][0].value,
                )
            else:
                # Create a new field type if it does not exist
                field_type_dao.create_field_type(
                    project_id,
                    field_name,
                    existing_logs[0][0].value,
                )

        else:  # If we want to turn typing off
            field_type_dao.delete_field_type(project_id, field_name)

    return {"info": "Field types updated successfully!"}


#####################
# GroupBy Utils     #
#####################


def _get_distinct_group_values(
    log_event_ids: List[int],
    group_key: str,
    session,
) -> List[Any]:
    """Get distinct values for a group key among provided log event IDs."""

    subquery = (
        session.query(
            Log.value,
            Log.log_event_id,
            func.row_number()
            .over(
                partition_by=Log.value,
                order_by=desc(Log.log_event_id),
            )
            .label("rn"),
        )
        .filter(Log.log_event_id.in_(log_event_ids))
        .filter(Log.key == group_key)
        .subquery()
    )

    query = (
        session.query(subquery.c.value)
        .filter(subquery.c.rn == 1)
        .order_by(desc(subquery.c.log_event_id))
    )

    return [row[0] for row in query.all()]


def _get_log_event_ids_for_group_value(
    log_event_ids: List[int],
    group_key: str,
    group_value: Any,
    session,
) -> List[int]:
    """Get log event IDs that match a specific group value."""
    query = (
        session.query(Log.log_event_id)
        .filter(Log.log_event_id.in_(log_event_ids))
        .filter(Log.key == group_key)
        .filter(cast(Log.value, JSONB) == cast(group_value, JSONB))
    )
    return [row[0] for row in query.all()]


def _get_params_for_log_events(
    log_event_ids: List[int],
    session,
) -> Dict[str, Dict[int, Any]]:
    """Get all parameter versions used across the log events."""
    query = (
        session.query(Log)
        .filter(Log.log_event_id.in_(log_event_ids))
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
    session=Depends(get_db_session),
) -> Tuple[List[int], int]:
    """
    Return all log_event_ids (no pagination, no field-level filtering) that match
    these top-level filters: from_ids, exclude_ids, filter_expr, context, and project.

    Returns:
        (event_ids, total_count)
    """
    user_id = request_fastapi.state.user_id

    # Validate project
    try:
        project_obj = project_dao.filter(name=project, user_id=user_id)[0][0]
    except IndexError:
        raise HTTPException(status_code=404, detail=f"Project {project} not found.")
    project_id = project_obj.id

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

    # Handle user-defined filter_expr => build SQL expression on LogEvent
    if filter_expr:
        filter_dict = str_filter_exp_to_dict(filter_expr)
        if filter_dict:
            condition = build_sql_query(filter_dict, LogEvent, session)
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
        ctx_id = context_dao.filter(name=context, project_id=project_id)
        if ctx_id:
            ctx_id_val = ctx_id[0][0].id
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

    # Execute the query: we get all relevant event IDs (no limit/offset)
    all_ids = log_event_query.all()  # each row is a tuple (id,)
    event_ids = [r[0] for r in all_ids]
    total_count = len(event_ids)

    return event_ids, total_count


def _fetch_logs_for_event_ids(
    request_fastapi: Request,
    event_ids: List[int],
    column_context: Optional[str],
    from_fields: Optional[str],
    exclude_fields: Optional[str],
    sorting: Optional[str],
    limit: Optional[int],
    offset: int,
    parent_fields: Optional[str],
    project_dao: ProjectDAO,
    field_type_dao: FieldTypeDAO,
    session=Depends(get_db_session),
    latest_timestamp: bool = False,
) -> Union[Tuple[List[Tuple[Union[Log, DerivedLog], datetime, int]], int], str]:
    """
    Given a known list of event_ids, retrieve the union of Log + DerivedLog rows
    that match column_context, from_fields/exclude_fields, etc. Then apply sorting
    + pagination to the distinct event_ids, and return (rows, count).
    If latest_timestamp=True, return only the max updated_at across those logs.
    """
    if not event_ids:
        return ([], 0) if not latest_timestamp else None

    user_id = request_fastapi.state.user_id

    # Quick project check to retrieve field type info
    # (We assume the user is allowed to see these events.)
    try:
        project_obj = project_dao.filter(user_id=user_id)[0][0]
        project_id = project_obj.id
    except IndexError:
        raise HTTPException(status_code=404, detail="Project not found.")

    # 1) Build union subquery from base logs + derived logs, for these event IDs
    base_logs_q = (
        session.query(
            Log.id.label("id"),
            Log.log_event_id.label("log_event_id"),
            Log.key.label("key"),
            Log.value.label("value"),
            Log.inferred_type.label("inferred_type"),
            Log.version.label("version"),
            Log.updated_at.label("updated_at"),
            LogEvent.created_at.label("created_at"),
            literal("base").label("source_type"),
        )
        .join(LogEvent, LogEvent.id == Log.log_event_id)
        .filter(LogEvent.id.in_(event_ids))
    )

    derived_logs_q = (
        session.query(
            DerivedLog.id.label("id"),
            DerivedLog.log_event_id.label("log_event_id"),
            DerivedLog.key.label("key"),
            DerivedLog.value.label("value"),
            DerivedLog.inferred_type.label("inferred_type"),
            cast(None, Integer).label("version"),
            DerivedLog.updated_at.label("updated_at"),
            LogEvent.created_at.label("created_at"),
            literal("derived").label("source_type"),
        )
        .join(LogEvent, LogEvent.id == DerivedLog.log_event_id)
        .filter(LogEvent.id.in_(event_ids))
    )

    unified_logs_subq = base_logs_q.union_all(derived_logs_q).subquery("unified_logs")

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

    if exclude_params:
        filtered_logs_q = filtered_logs_q.filter(unified_logs_subq.c.version.is_(None))
    elif exclude_entries:
        filtered_logs_q = filtered_logs_q.filter(
            unified_logs_subq.c.version.isnot(None),
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

    field_types = field_type_dao.get_field_types(project_id)
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
                    sort_expr = cast(key_subq.c.raw_value, cast_type)
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
        row_version,
        row_created_at,
        row_source_type,
    ) in raw_rows:
        results.append(
            (
                row_key,
                row_value,
                row_inferred_type,
                row_version,
                row_source_type,
                row_created_at,
                row_event_id,
            ),
        )

    return results, context_len, total_count


def _build_grouped_data(
    request_fastapi: Request,
    project: str,
    log_event_ids: List[int],
    field_order_map: Dict[str, int],
    group_by: List[str],
    group_depth: Optional[int],
    group_limit: Optional[int],
    group_offset: int,
    level: int,
    limit: Optional[int],
    offset: int,
    column_context: Optional[str],
    from_fields: Optional[str],
    exclude_fields: Optional[str],
    sorting: Optional[str],
    project_dao: ProjectDAO,
    field_type_dao: FieldTypeDAO,
    context_dao: ContextDAO,
    session=Depends(get_db_session),
    value_limit: Optional[int] = None,
    parent_group_key: Optional[str] = "",
) -> Dict[str, Any]:
    """
    Returns a dict shaped like:
      {
        "<current_group_key>": {
          <group_value1 or "null">: <substructure or leaf logs>,
          <group_value2>: <substructure or leaf logs>,
          ...
          "group_count": int,
          "count": int
        }
      }

    If level >= group_depth or we've exhausted group_by, we return the leaf logs (list).
    """

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
            for k, v in sub_val.items():
                if k not in ("group_count", "count"):
                    total += _get_count_from_substructure(v)
            return total
        else:
            return 0

    def parse_group_key(key: str) -> Tuple[str, str]:
        """Returns (prefix, actual_field). e.g. 'entries/i' -> ('entries','i')."""
        parts = key.split("/", 1)
        if len(parts) == 1:
            return ("", key)
        return (parts[0], parts[1])

    total_logs_in_group = len(log_event_ids)
    # If no logs, return empty
    if total_logs_in_group == 0:
        return {}

    # If we've run out of group_by keys OR group_depth
    # => fetch the actual logs (leaf)
    if level >= len(group_by):
        rows, context_len, leaf_count = _fetch_logs_for_event_ids(
            request_fastapi=request_fastapi,
            event_ids=log_event_ids,
            column_context=column_context,
            from_fields=from_fields,
            exclude_fields=exclude_fields,
            sorting=sorting,
            limit=limit,
            offset=offset,
            parent_fields=parent_group_key,
            project_dao=project_dao,
            field_type_dao=field_type_dao,
            session=session,
        )
        logs_out, _ = _format_flat_logs(rows, context_len, value_limit, field_order_map)
        return logs_out  # A list of logs

    current_group_key = group_by[level]
    prefix, raw_key = parse_group_key(current_group_key)
    # 1) Distinguish logs that *have* this group_key vs. logs that are missing it
    #    (We put missing ones in the "null" group).
    present_values = _get_distinct_group_values(
        session=session,
        log_event_ids=log_event_ids,
        group_key=raw_key,
    )

    # If group_depth is specified AND we have reached it,
    if group_depth is not None and level >= group_depth:
        # For depth=0, we need to maintain the structure but only return counts
        if level == 0:
            return {
                current_group_key: {
                    "group_count": len(present_values),
                    "count": total_logs_in_group,
                },
            }
        return total_logs_in_group

    # This is a list of distinct values that exist.

    # 2) Build subsets for each distinct value
    #    But also compute the set of all IDs that appear in these subsets
    #    so we can figure out which are missing
    used_ids = set()
    value_to_ids = {}

    for val in present_values:
        subset_ids = _get_log_event_ids_for_group_value(
            session=session,
            log_event_ids=log_event_ids,
            group_key=raw_key,
            group_value=val,
        )
        value_to_ids[val] = subset_ids
        used_ids.update(subset_ids)

    # 3) "null" group => all log IDs that do not have the group_key
    missing_ids = set(log_event_ids) - used_ids
    have_null = len(missing_ids) > 0

    # 4) Apply group_offset & group_limit to the distinct values (but not to "null")
    total_distinct = len(present_values)
    if group_limit is not None:
        paged_values = present_values[group_offset : group_offset + group_limit]
    else:
        paged_values = present_values

    # Build the data structure that will go inside e.g.  "params/a/b/param2": {...}
    out_dict = {}
    # We will fill out_dict[<value>] = substructure or logs
    # then compute out_dict["count"] and out_dict["group_count"]

    # 5) Recurse on each distinct value
    #    The substructure might be a list (leaf logs) or a nested dict
    #    We store them in a dictionary keyed by that value
    for val in paged_values:
        subset = value_to_ids[val]
        sub = _build_grouped_data(
            request_fastapi=request_fastapi,
            project=project,
            log_event_ids=list(subset),
            field_order_map=field_order_map,
            group_by=group_by,
            group_depth=group_depth,
            group_limit=group_limit,
            group_offset=group_offset,
            level=level + 1,
            limit=limit,
            offset=offset,
            column_context=column_context,
            from_fields=from_fields,
            exclude_fields=exclude_fields,
            sorting=sorting,
            project_dao=project_dao,
            field_type_dao=field_type_dao,
            context_dao=context_dao,
            session=session,
            value_limit=value_limit,
            parent_group_key="&".join([parent_group_key, raw_key])
            if parent_group_key
            else raw_key,
        )
        out_dict[val] = sub

    # 6) If we have missing_ids => we create a "null" group
    if have_null:
        null_sub = _build_grouped_data(
            request_fastapi=request_fastapi,
            project=project,
            log_event_ids=list(missing_ids),
            field_order_map=field_order_map,
            group_by=group_by,
            group_depth=group_depth,
            group_limit=group_limit,
            group_offset=group_offset,
            level=level + 1,
            limit=limit,
            offset=offset,
            column_context=column_context,
            from_fields=from_fields,
            exclude_fields=exclude_fields,
            sorting=sorting,
            project_dao=project_dao,
            field_type_dao=field_type_dao,
            context_dao=context_dao,
            session=session,
            value_limit=value_limit,
        )
        out_dict["null"] = null_sub

    # 7) Compute "count" = sum of substructures' counts
    total_count_sub = 0
    for k, sub_val in out_dict.items():
        if k not in ("group_count", "count"):
            total_count_sub += _get_count_from_substructure(sub_val)

    # 8) group_count = # distinct values + 1 if we have a "null" group
    computed_group_count = total_distinct

    # 9) Put them into out_dict
    out_dict["group_count"] = computed_group_count
    out_dict["count"] = total_count_sub

    # 10) Finally, wrap this under the current_group_key:
    #     e.g. { "params/a/b/param2": out_dict }
    return {
        current_group_key: out_dict,
    }
