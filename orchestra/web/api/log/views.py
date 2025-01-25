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
    UpdateLogRequest,
)
from orchestra.web.api.utils.http_responses import not_found

from .helpers import (
    STR_TO_SQL_TYPES,
    _compute_expression,
    _flatten_fields,
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

    def enforce_types(field_name, value):
        entered_type = LogDAO.infer_type(field_name, value)
        expected_type = field_types.get(field_name)
        if expected_type:
            if expected_type == "NoneType":
                if entered_type == "NoneType":
                    return
                # update the field type to the new type
                field_type_dao.update_field_type(project_id, field_name, value)
            elif entered_type != expected_type:
                raise HTTPException(
                    status_code=400,
                    detail=f"Type mismatch for field '{field_name}': expected {expected_type}, got {entered_type}",
                )
        else:
            field_type_dao.create_field_type(project_id, field_name, value)

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
            enforce_types(k, v)
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
            enforce_types(k, v)
            log_dao.create_from_raw_k_v(
                project_id=project_id,
                log_event_id=log_event_id,
                raw_k=k,
                raw_v=v,
                explicit_types=entries_explicit_types,
            )

    return log_event_ids


@router.put(
    "/log/derived",
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
            logs, _, _count = _get_logs_query(
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
            # logs is a list of (Log, created_at, log_event_id) or (DerivedLog,...),
            # we only want distinct log_event_id
            le_ids = list({r[2] for r in logs})
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
        inferred_type = LogDAO.infer_type("", computed_values[0][1])

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
        field_type_dao.create_field_type(project_obj.id, body.key, val)
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
                        field_type_dao.update_field_type(project_id, k, v)
                    elif original_type != expected_type:
                        raise HTTPException(
                            status_code=400,
                            detail=f"Type mismatch for field '{k}': expected {expected_type}, got {original_type}",
                        )
                else:
                    field_type_dao.create_field_type(project_id, k, v)

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


def _get_base_logs_subq(
    project_id: int,
    filter_expr: Optional[str],
    from_ids: Optional[str],
    exclude_ids: Optional[str],
    column_context: Optional[str],
    context: Optional[str],
    from_fields: Optional[str],
    exclude_fields: Optional[str],
    context_dao: ContextDAO,
    session=Depends(get_db_session),
) -> Subquery:
    """
    Builds a SQLAlchemy query for base logs (Log + LogEvent) with user filters,
    and returns it as a subquery.
    """
    # First build query for LogEvent filtering
    event_query = session.query(LogEvent.id).filter(LogEvent.project_id == project_id)

    # Handle ID filtering
    if from_ids and exclude_ids:
        raise HTTPException(
            status_code=400,
            detail="Cannot set both from_ids and exclude_ids.",
        )

    if from_ids:
        include_ids = [int(x) for x in from_ids.split("&")]
        event_query = event_query.filter(LogEvent.id.in_(include_ids))
    elif exclude_ids:
        exclude_set = [int(x) for x in exclude_ids.split("&")]
        event_query = event_query.filter(LogEvent.id.notin_(exclude_set))

    # Handle filter expression
    if filter_expr:
        filter_dict = str_filter_exp_to_dict(filter_expr)
        if filter_dict:
            condition = build_sql_query(filter_dict, LogEvent, session)
            if isinstance(condition, Subquery):
                event_query = event_query.filter(
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
                event_query = event_query.filter(condition)

    # Now build the final query with Log join
    q = (
        session.query(
            Log.id.label("id"),
            Log.log_event_id.label("log_event_id"),
            Log.key.label("key"),
            Log.value.label("value"),
            Log.inferred_type.label("inferred_type"),
            Log.version.label("version"),
            Log.updated_at.label("updated_at"),
            LogEvent.created_at.label("created_at"),
            cast(None, JSONB).label("referenced_logs"),
            literal("base").label("source_type"),
        )
        .join(LogEvent, LogEvent.id == Log.log_event_id)
        .filter(LogEvent.id.in_(event_query))
    )

    # Handle context filtering
    context_len = 0
    if column_context is not None:
        split_context = column_context.split("/")
        exclude_params = "entries" in split_context
        exclude_entries = "params" in split_context
        assert not (
            exclude_params and exclude_entries
        ), "'entries' and 'params' cannot both be specified in the context argument."
        column_context = "/".join(
            [substr for substr in split_context if substr not in ("params", "entries")],
        )
        if column_context:
            column_context = (
                column_context if column_context[-1] == "/" else column_context + "/"
            )
            context_len = len(column_context)
            q = q.where(Log.key.startswith(column_context))
        if exclude_params:
            q = q.where(Log.version.is_(None))
        elif exclude_entries:
            q = q.where(Log.version.isnot(None))

    # Filter by static context if provided
    if context:
        context_id = context_dao.filter(name=context, project_id=project_id)
        if context_id:
            # Join with log_event_context table to filter by context
            q = q.join(
                LogEventContext,
                LogEventContext.log_event_id == LogEvent.id,
                isouter=False,
            ).where(
                LogEventContext.context_id == context_id[0][0].id,
            )

    # Handle field filtering
    assert not (from_fields and exclude_fields), (
        f"Only one of from_fields or exclude_fields can be set, "
        f"but found values {from_fields} and {exclude_fields}."
    )
    if from_fields:
        q = q.where(Log.key.in_(from_fields.split("&")))
    elif exclude_fields:
        q = q.where(Log.key.notin_(exclude_fields.split("&")))

    return q.subquery(name="base_logs_subq"), context_len


def _get_derived_logs_subq(
    base_subq: Subquery,
    session=Depends(get_db_session),
) -> Subquery:
    """
    Given a subquery of base logs (already filtered), return a subquery of
    *derived logs* that reference at least one row in base_subq.

    The columns must match base_subq for union_all to work. So we return:
      id, log_event_id, key, value, inferred_type, version, created_at, referenced_logs, source_type
    """
    # We'll join DerivedLog -> LogEvent, and then join base_subq on the
    # condition that the derived log references base_subq's (key -> log_event_id).
    q = (
        session.query(
            DerivedLog.id.label("id"),
            DerivedLog.log_event_id.label("log_event_id"),
            DerivedLog.key.label("key"),
            DerivedLog.value.label("value"),
            DerivedLog.inferred_type.label("inferred_type"),
            # derived logs have no version, so cast(None, Integer)
            cast(None, Integer).label("version"),
            DerivedLog.updated_at.label("updated_at"),
            LogEvent.created_at.label("created_at"),
            DerivedLog.referenced_logs.label("referenced_logs"),
            literal("derived").label("source_type"),
        )
        .join(LogEvent, LogEvent.id == DerivedLog.log_event_id)
        # Now the crucial join to base_subq
        .join(
            base_subq,
            and_(
                # Compare JSONB ->> base_subq.c.key to base_subq.c.log_event_id
                cast(DerivedLog.referenced_logs[base_subq.c.key].astext, Integer)
                == base_subq.c.log_event_id,
            ),
        )
        # If a derived log references multiple different base logs, it might appear multiple times;
        # so we can ensure uniqueness with .distinct(DerivedLog.id)
        .distinct(DerivedLog.id)
        .subquery(name="derived_logs_subq")
    )
    return q


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
    session,
    latest_timestamp=False,
):
    """
    Returns a combined list of base logs and derived logs that match user filters.
    Each row is (Log or DerivedLog object, created_at, log_event_id).
    """
    user_id = request_fastapi.state.user_id

    # --- 1) Validate the project
    try:
        project_obj = project_dao.filter(name=project, user_id=user_id)[0][0]
    except IndexError:
        raise not_found(f"Project {project}")
    project_id = project_obj.id

    # --- 2) Build subqueries for base logs and derived logs
    base_subq, context_len = _get_base_logs_subq(
        project_id=project_id,
        filter_expr=filter_expr,
        from_ids=from_ids,
        exclude_ids=exclude_ids,
        column_context=column_context,
        context=context,
        from_fields=from_fields,
        exclude_fields=exclude_fields,
        context_dao=context_dao,
        session=session,
    )
    # 2) Build derived_subq, passing base_subq to filter by references
    derived_subq = _get_derived_logs_subq(
        base_subq=base_subq,
        session=session,
    )

    # union_q will have columns:
    # id, log_event_id, key, value, inferred_type, version, created_at, source_type
    union_q = (
        session.query(
            base_subq.c.id.label("id"),
            base_subq.c.log_event_id.label("log_event_id"),
            base_subq.c.key.label("key"),
            base_subq.c.value.label("value"),
            base_subq.c.inferred_type.label("inferred_type"),
            base_subq.c.version.label("version"),
            base_subq.c.created_at.label("created_at"),
            base_subq.c.updated_at.label("updated_at"),
            base_subq.c.referenced_logs.label("referenced_logs"),
            base_subq.c.source_type.label("source_type"),
        )
        .union_all(
            session.query(
                derived_subq.c.id.label("id"),
                derived_subq.c.log_event_id.label("log_event_id"),
                derived_subq.c.key.label("key"),
                derived_subq.c.value.label("value"),
                derived_subq.c.inferred_type.label("inferred_type"),
                derived_subq.c.version.label("version"),
                derived_subq.c.created_at.label("created_at"),
                derived_subq.c.updated_at.label("updated_at"),
                derived_subq.c.referenced_logs.label("referenced_logs"),
                derived_subq.c.source_type.label("source_type"),
            ),
        )
        .subquery(name="union_q")
    )

    # --- 3) Build a subquery of distinct log_event_ids that are actually present
    # in the union (i.e. they matched context, filter_expr, from_fields, etc.).
    distinct_ids_subq = (
        session.query(union_q.c.log_event_id.label("log_event_id"))
        .distinct()
        .subquery(name="distinct_ids_subq")
    )

    # --- 4) Sorting
    # for each user-defined key, we create a subquery
    # that grabs (log_event_id, value) from the union for that particular key.
    # Then we cast the value to a known type (using the field_type table).
    sorted_query = session.query(distinct_ids_subq.c.log_event_id)
    sort_criteria = []

    # We'll store subqueries for each key so we can outerjoin them
    subqs_for_sort = {}
    field_types = field_type_dao.get_field_types(
        project_id,
    )

    if sorting:
        # e.g. sorting='{"score":"ascending","timestamp":"descending"}'
        sort_dict = json.loads(sorting)
        #
        # 4a) For each user-specified sort key, we create a sub-subquery that merges:
        #     - base logs that have key=<sort_key>
        #     - derived logs that reference <sort_key> -> base log (join to that base log's value)
        #
        for sort_key, _mode in sort_dict.items():
            # build a subquery of shape (log_event_id, cast(value) AS val_for_key)
            # Subquery #1: base logs that literally have key=sort_key
            base_sort_subq = (
                session.query(
                    union_q.c.log_event_id.label("log_event_id"),
                    # We'll store the raw_value in a column
                    union_q.c.value.label("raw_value"),
                )
                .filter(union_q.c.source_type == "base")
                .filter(union_q.c.key == sort_key)
                .subquery(f"base_{sort_key}_subq")
            )

            # Subquery #2: derived logs that reference sort_key -> a base log_event_id
            # We parse the JSON and get the base log_event_id. Then we join to Log to get the base log's .value
            derived_ref_subq = (
                session.query(
                    union_q.c.log_event_id.label("log_event_id"),
                    func.cast(
                        func.jsonb_extract_path_text(
                            union_q.c.referenced_logs,
                            sort_key,
                        ),
                        Integer,
                    ).label("base_log_event_id"),
                )
                .filter(union_q.c.source_type == "derived")
                .filter(func.jsonb_exists(union_q.c.referenced_logs, sort_key))
                .subquery(f"derived_{sort_key}_ref_subq")
            )

            # Adjust the derived_with_base_subq to ensure correct join and filtering
            derived_with_base_subq = (
                session.query(
                    derived_ref_subq.c.log_event_id.label("log_event_id"),
                    Log.value.label("raw_value"),
                )
                .join(
                    Log,
                    and_(
                        Log.log_event_id == derived_ref_subq.c.base_log_event_id,
                        Log.key == sort_key,
                    ),
                    isouter=False,
                )
                .subquery(f"derived_{sort_key}_joined_subq")
            )

            # Merge them (UNION) so that we have one subquery for "sort_key"
            # that yields (log_event_id, raw_value)
            union_sort_subq = (
                session.query(
                    base_sort_subq.c.log_event_id.label("log_event_id"),
                    base_sort_subq.c.raw_value.label("raw_value"),
                )
                .union(
                    session.query(
                        derived_with_base_subq.c.log_event_id.label("log_event_id"),
                        derived_with_base_subq.c.raw_value.label("raw_value"),
                    ),
                )
                .subquery(f"sort_{sort_key}_union_subq")
            )

            subqs_for_sort[sort_key] = union_sort_subq

        # Now we outerjoin each subq onto sorted_query
        for key, mode in sort_dict.items():
            if mode not in ("ascending", "descending"):
                raise HTTPException(
                    status_code=400,
                    detail=f"Sort mode must be 'ascending' or 'descending', got {mode}.",
                )
            direction = asc if mode == "ascending" else desc
            subq = subqs_for_sort[key]

            # Outer join
            sorted_query = sorted_query.outerjoin(
                subq,
                subq.c.log_event_id == distinct_ids_subq.c.log_event_id,
            )

            # 4b) Cast the subq.c.raw_value based on the field type (if known)
            if key in field_types:
                python_type = field_types[key]
                cast_type = STR_TO_SQL_TYPES[python_type]

                # Now build an expression for sorting
                sort_expr = (
                    cast(
                        cast(subq.c.raw_value, String),
                        cast_type,
                    )
                    if cast_type == DateTime
                    else cast(subq.c.raw_value, cast_type)
                )
            else:
                # fallback: treat it as text
                sort_expr = subq.c.raw_value

            # For null handling:
            sort_criteria.append(direction(sort_expr).nulls_last())

    # --- 4c) Always fallback to sorting by log_event_id desc if not explicitly sorting by id
    if not sorting or "id" not in sorting:
        sort_criteria.append(distinct_ids_subq.c.log_event_id.desc())

    # --- 4d) row_number() approach
    sorted_query = sorted_query.add_columns(
        func.row_number().over(order_by=sort_criteria).label("row_num"),
    ).order_by("row_num")

    # --- 5) Pagination
    count = sorted_query.count()  # total number of log_event_ids
    if limit:
        sorted_query = sorted_query.limit(limit)
    if offset:
        sorted_query = sorted_query.offset(offset)

    # We'll subquery this so we can join back to union_q
    final_ids_subq = sorted_query.subquery(name="final_ids_subq")
    # final_ids_subq has columns: log_event_id, row_num

    # --- 6) If user only wants latest_timestamp
    if latest_timestamp:
        max_updated_at = (
            session.query(func.max(union_q.c.updated_at))
            .join(
                final_ids_subq,
                final_ids_subq.c.log_event_id == union_q.c.log_event_id,
            )
            .scalar()
        )
        return max_updated_at.isoformat() if max_updated_at else None

    # --- 7) Now fetch the actual rows by joining union_q to final_ids_subq
    # so we only get the subset of log_event_ids that survived the pagination.
    logs_query = (
        session.query(
            union_q.c.id,
            union_q.c.log_event_id,
            union_q.c.key,
            union_q.c.value,
            union_q.c.inferred_type,
            union_q.c.version,
            union_q.c.created_at,
            union_q.c.source_type,
            final_ids_subq.c.row_num,
        )
        .join(final_ids_subq, final_ids_subq.c.log_event_id == union_q.c.log_event_id)
        .order_by(final_ids_subq.c.row_num, union_q.c.created_at)
    )

    rows = logs_query.all()

    # rows now is a list of:
    # [
    #   (
    #       id, log_event_id, key, value, inferred_type, version,
    #       created_at, source_type, row_num
    #   ), ...
    # ]

    # --- 8) Re-hydrate them into Log or DerivedLog
    results = []
    for row in rows:
        if row.source_type == "base":
            obj = session.query(Log).get(row.id)
        else:
            obj = session.query(DerivedLog).get(row.id)

        if obj:
            results.append((obj, row.created_at, row.log_event_id))

    # Return the same format: (list_of_rows, context_len, count)
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
    return_ids_only: bool = False,
    project_dao: ProjectDAO = Depends(),
    field_type_dao: FieldTypeDAO = Depends(),
    context_dao: ContextDAO = Depends(),
    session=Depends(get_db_session),
):
    """
    Returns a list of filtered entries from a project. When group_threshold is set,
    entries that appear in at least that many logs will be grouped together in the
    grouped_entries field to reduce response size.

    The response will include:
    - logs: List of log entries with their individual values
    - params: Dictionary of parameter versions
    - count: Total number of logs
    - grouped_entries: (When group_threshold is set) Dictionary of field names to their shared values
    """
    all_rows, context_len, count = _get_logs_query(
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
    # all_rows is now a list of (Log|DerivedLog, created_at, log_event_id)
    if return_ids_only:
        event_ids = [r[2] for r in all_rows]
        return list(dict.fromkeys(event_ids))

    # Format them
    formatted = {}

    # Get ordered field names
    user_id = request_fastapi.state.user_id
    project_id = project_dao.filter(name=project, user_id=user_id)[0][0].id
    field_order_map = field_type_dao.get_ordered_field_names(project_id)
    for row_obj, created_at, event_id in all_rows:
        if event_id not in formatted:
            formatted[event_id] = {
                "ts": created_at.isoformat() if created_at else None,
                "clipped_fields": [],
                "entries": {},
                "versions": {},
                "derived_entries": {},
            }
        is_derived = isinstance(row_obj, DerivedLog)

        # Apply context_len slicing to the key
        key = row_obj.key[context_len:]

        def _limit_value(value: Any, inferred_type: str) -> Tuple[Any, bool]:
            """Limit the size of a value based on its type and the value_limit parameter.
            Returns a tuple of (limited_value, is_clipped)."""
            if value_limit is None:
                return value, False

            # Handle numeric values - return as is
            if inferred_type in ["int", "float", "bool"]:
                return value, False

            # Handle image fields - return empty string
            if inferred_type == "image":
                return "", True

            # Convert value to string if it's a nested structure
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

        # noinspection PyBroadException
        def _try_decode(str_in):
            try:
                return json.loads(str_in)
            except:
                return str_in

        val = (
            _try_decode(row_obj.value)
            if isinstance(row_obj.value, str)
            else row_obj.value
        )

        # Apply value limiting and get clipped status
        limited_val, is_clipped = _limit_value(val, row_obj.inferred_type)
        if is_clipped:
            formatted[event_id]["clipped_fields"] = formatted[event_id].get(
                "clipped_fields",
                [],
            ) + [key]

        ver = getattr(row_obj, "version", None)

        if is_derived:
            # --- Handle derived Log
            assert (
                key not in formatted[event_id]["derived_entries"]
            ), f"found duplicate derived key {key} with log_id {event_id}"

            formatted[event_id]["derived_entries"][key] = limited_val

        else:
            # --- Handle base Log
            assert (
                key not in formatted[event_id]["entries"]
            ), f"found duplicates for key {key} with log_id {event_id}"

            if ver is None:
                # Put in "entries"
                formatted[event_id]["entries"][key] = limited_val
            else:
                # Put in "params"
                if key not in formatted[event_id]["versions"]:
                    formatted[event_id]["versions"][key] = {}
                formatted[event_id]["versions"][key][ver] = limited_val
                formatted[event_id]["entries"][key] = str(ver)

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

        logs_out.append(
            {
                "id": event_id,
                "ts": data["ts"],
                "entries": sorted_entries,
                "params": sorted_params,
                "derived_entries": sorted_derived,
                "clipped_fields": data.get("clipped_fields", []),
            },
        )

    return {
        "params": params_out,
        "logs": logs_out,
        "count": count,
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
    project_dao: ProjectDAO = Depends(),
    log_event_dao: LogEventDAO = Depends(),
    log_dao: LogDAO = Depends(),
) -> Dict[str, Any]:
    """
    Returns a dict with the different versions as keys and the values of the remaining
    items within a given project based on its key.
    """
    try:
        user_id = request_fastapi.state.user_id
        project_obj = project_dao.filter(name=project, user_id=user_id)[0][0]
    except IndexError:
        raise not_found(f"Project {project}")
    # TODO: Deal with organisation IDs
    log_events = log_event_dao.filter(project_id=project_obj.id)
    all_entries = log_dao.filter(log_event_id=[le[0].id for le in log_events], key=key)
    groups = dict()
    for entry in all_entries:
        # TODO: Add pagination
        version = entry[0].version
        value = entry[0].value
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
    query = (
        session.query(Log.key)
        .join(LogEvent, LogEvent.id == Log.log_event_id)
        .filter(LogEvent.project_id == project_obj.id)
        .distinct()
    )

    all_field_names = "&".join([field.key for field in query.all()])

    # ToDo: remove this hacky code once this task [https://app.clickup.com/t/86c1jupp2]
    #  is done
    (all_logs, _, _) = _get_logs_query(
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
    field_types = dict(
        (
            lg[0].key,
            (
                "derived_entry"
                if isinstance(lg[0], DerivedLog)
                else "entry"
                if lg[0].version is None
                else "param"
            ),
        )
        for lg in all_logs
    )
    # end ToDo

    # return field types in the same order as they were created
    return {
        key: {
            "data_type": types.get(key),
            "field_type": field_types.get(key),
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
                field_type_dao.update_field_type(
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
