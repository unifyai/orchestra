"""
Includes endpoints related to entries.
"""

import json
from typing import Any, Dict, List, Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from sqlalchemy import INTEGER, Float, case, cast, desc, func, select
from sqlalchemy.dialects.postgresql import BOOLEAN, JSONB

from orchestra.db.dao.field_type_dao import FieldTypeDAO
from orchestra.db.dao.log_dao import LogDAO, OverwriteError
from orchestra.db.dao.log_event_dao import LogEventDAO
from orchestra.db.dao.project_dao import ProjectDAO
from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import Log, LogEvent
from orchestra.web.api.log.schema import (
    CreateLogConfig,
    DeleteLogEntryRequest,
    DeleteLogsRequest,
    SetFieldTypingRequest,
    UpdateLogRequest,
)
from orchestra.web.api.utils.http_responses import not_found

from .helpers import _flatten_fields, build_filter, format_logs, str_filter_exp_to_dict

router = APIRouter()

###########################
# endpoints
###########################


@router.post(
    "/log",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {"info": "Log created successfully!"},
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
def create_log(
    request_fastapi: Request,
    request: CreateLogConfig,
    project_dao: ProjectDAO = Depends(),
    field_type_dao: FieldTypeDAO = Depends(),
    log_event_dao: LogEventDAO = Depends(),
    log_dao: LogDAO = Depends(),
):
    """
    Creates a log associated to a project. Logs are
    LLM-call-level data that might depend on other variables.

    A "explicit_types" dictionary can be passed as part of the `entries`.
    If present, any matching key inside this dictionary will override the
    inferred type of that particular entry.

    This method returns the id of the new stored log.
    """
    # check if the project exists
    try:
        # TODO: Add organization id
        user_id = request_fastapi.state.user_id
        project = project_dao.filter(user_id=user_id, name=request.project)
        project_id = project[0][0].id
    except IndexError:
        raise not_found("Project")

    # Create log_event and get its id
    log_event_id = log_event_dao.create(project_id=project_id)

    entries_explicit_types = request.entries.pop("explicit_types", None)
    params_explicit_types = request.params.pop("explicit_types", None)
    field_types = field_type_dao.get_field_types(project_id)
    strongly_typed = request.strongly_typed
    entries = request.entries
    params = request.params

    def enforce_types(field_name, value):
        if field_name in field_types:
            expected_type = field_types[field_name]
            original_type = LogDAO.infer_type(value)
            if original_type != expected_type:
                raise HTTPException(
                    status_code=400,
                    detail=f"Type mismatch for field '{field_name}': expected {expected_type}, got {original_type}",
                )
        else:
            # If strongly_typed is True, set the type for the first entry
            if strongly_typed is True or (
                isinstance(strongly_typed, list) and field_name in strongly_typed
            ):
                field_type_dao.create_field_type(project_id, field_name, value)

    for k, v in params.items():
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

    # Store each key, value entry pair for the log
    for k, v in entries.items():
        enforce_types(k, v)
        log_dao.create_from_raw_k_v(
            project_id=project_id,
            log_event_id=log_event_id,
            raw_k=k,
            raw_v=v,
            explicit_types=entries_explicit_types,
        )

    return log_event_id


@router.delete(
    "/logs",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {"info": "Logs deleted successfully!"},
                },
            },
        },
        404: {
            "description": "Logs Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "One or more logs with the specified IDs were not found.",
                    },
                },
            },
        },
    },
)
def delete_logs(
    request_fastapi: Request,
    body: DeleteLogsRequest,
    log_event_dao: LogEventDAO = Depends(),
):
    """
    Deletes multiple logs from a project.
    """
    not_found_ids = []
    for log_id in body.ids:
        try:
            if log_event_dao.get_user_id(id=log_id) != request_fastapi.state.user_id:
                raise IndexError
        except IndexError:
            not_found_ids.append(log_id)
            continue
        # TODO: Deal with organisation IDs
        log_event_dao.delete(id=log_id)

    if not_found_ids:
        raise HTTPException(
            status_code=404,
            detail=f"Logs with ids {not_found_ids} not found or you don't have permission to delete them.",
        )

    return {"info": "Logs deleted successfully!"}


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
):
    """
    Updates multiple logs with the provided entries. Each entry will be either added
    or overridden in the specified logs.

    A dictionary of "explicit_types" can be passed as part of the `entries`.
    If present, it will override the inferred type of any matching key in all logs.
    """
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
            strongly_typed = body.strongly_typed
            for k, v in this_data.items():
                # Check and enforce types
                if k in field_types:
                    expected_type = field_types[k]
                    original_type = LogDAO.infer_type(v)
                    if original_type != expected_type:
                        raise HTTPException(
                            status_code=400,
                            detail=f"Type mismatch for field '{k}': expected {expected_type}, got {original_type}",
                        )
                else:
                    # If strongly_typed is True, set the type for the first entry
                    if strongly_typed is True or (
                        isinstance(strongly_typed, list) and k in strongly_typed
                    ):
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
    return {"info": "Logs updated successfully!"}


@router.delete(
    "/logs/fields",
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
def delete_log_fields(
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

    log_fields = _flatten_fields(body.fields)

    for log_id, fields in log_fields.items():
        # Verify if the log belongs to the user
        try:
            if log_event_dao.get_user_id(id=log_id) != request_fastapi.state.user_id:
                raise IndexError
        except IndexError:
            not_found_logs.append(log_id)
            continue

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

    return {"info": "Log field deleted successfully from all logs!"}


@router.get(
    "/log/{id}",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {
                        "id": "123",
                        "ts": "2024-10-30 12:20:03",
                        "entries": {"input": "...", "output": "..."},
                    },
                },
            },
        },
        404: {
            "description": "Log Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Log with id <id> not found.",
                    },
                },
            },
        },
    },
)
def get_log(
    request_fastapi: Request,
    id: int = Path(
        description="ID of the log to fetch.",
        example="123",
    ),
    log_event_dao: LogEventDAO = Depends(),
    log_dao: LogDAO = Depends(),
):
    """
    Returns the log associated with a given id.
    """
    # TODO: Change this one to return the params as well
    if log_event_dao.get_user_id(id=id) != request_fastapi.state.user_id:
        raise not_found(f"Log with id {id}")
    # TODO: Deal with organisation IDs

    ts = log_event_dao.get_ts(id=id)
    log_entries = log_dao.filter(log_event_id=id)
    params_map = {}
    log_params = {}
    entries = {}

    for l in log_entries:
        if l[0].version is not None:
            if l[0].key not in params_map:
                params_map[l[0].key] = dict()
            params_map[l[0].key][l[0].version] = json.loads(l[0].value)
            # json keys can't be int so leaving the value as str as well
            log_params[l[0].key] = str(l[0].version)
        else:
            entries[l[0].key] = json.loads(l[0].value)

    return {
        "params": params_map,
        "logs": {"id": id, "ts": ts, "entries": entries, "params": log_params},
    }


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
def get_logs(
    request_fastapi: Request,
    project: str = Query(
        description="Name of the project to get entries from.",
        example="eval-project",
    ),
    context: str = Query(
        None,
        description="The context (prepending '/' seperated field names) from which to "
        "retrieve the logs.",
        example="subjects/science/physics",
    ),
    filter_expr: Optional[str] = Query(
        None,
        description="Boolean string to filter entries. TODO: Detailed page.",
        example="len(output) > 200 and temperature == 0.5",
    ),
    limit: Optional[int] = Query(None, ge=1, le=200),
    offset: int = Query(0, ge=0),
    return_ids_only: bool = False,
    project_dao: ProjectDAO = Depends(),
    session=Depends(get_db_session),
):
    """
    Returns a list of filtered entries from a project.
    """
    try:
        user_id = request_fastapi.state.user_id
        project_obj = project_dao.filter(name=project, user_id=user_id)[0][0]
    except IndexError:
        raise not_found(f"Project {project}")
    # TODO: Deal with organisation IDs

    query = session.query(
        LogEvent.id,
        func.count(LogEvent.id).over().label("count"),
    ).where(LogEvent.project_id == project_obj.id)
    query = query.order_by(LogEvent.created_at)
    if filter_expr:
        filter_dict = str_filter_exp_to_dict(filter_expr)
        if filter_dict:
            condition = build_filter(filter_dict, LogEvent, session)
            query = query.filter(condition)

    if limit:
        query = query.limit(limit)
    if offset:
        query = query.offset(offset)

    relevant_logs = query.subquery()

    query = (
        session.query(
            Log,
            LogEvent.created_at.label("log_event_ts"),
            relevant_logs.c.count,
        )
        .join(
            LogEvent,
            LogEvent.id == Log.log_event_id,
        )
        .join(
            relevant_logs,
            relevant_logs.c.id == LogEvent.id,
        )
        .where(Log.log_event_id.in_(select(relevant_logs.c.id)))
        .order_by(Log.created_at)
    )

    context_len = 0
    if context:
        context = context if context[-1] == "/" else context + "/"
        context_len = len(context)
        query = query.where(Log.key.startswith(context))

    all_logs = query.all()
    if return_ids_only:
        return list(set([log[0].log_event_id for log in all_logs]))

    formatted_logs, count = format_logs(all_logs, context_len)

    params = dict()
    logs = []
    for log_event_id, log_dict in formatted_logs.items():

        for k, v in log_dict["entries"].items():
            if log_dict["versions"][k] is not None:
                if k not in params:
                    params[k] = dict()
                params[k][log_dict["versions"][k]] = v

        logs.append(
            {
                "id": log_event_id,
                "ts": log_dict["ts"],
                "entries": {
                    k: v
                    for k, v in log_dict["entries"].items()
                    if log_dict["versions"][k] is None
                },
                "params": {
                    k: str(log_dict["versions"][k])
                    for k, _ in log_dict["entries"].items()
                    if log_dict["versions"][k] is not None
                },
            },
        )

    return {
        "params": params,
        "logs": logs,
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
    filter_expr: Optional[str] = Query(
        None,
        description="Boolean string to filter entries. TODO: Detailed page.",
        example="len(output) > 200 and temperature == 0.5",
    ),
    limit: Optional[int] = Query(None, ge=1, le=200),
    offset: int = Query(0, ge=0),
    project_dao: ProjectDAO = Depends(),
    session=Depends(get_db_session),
):
    """
    Returns the update timestamp of the most recently updated log within the specified
    page and filter bounds.
    """
    try:
        user_id = request_fastapi.state.user_id
        project_obj = project_dao.filter(name=project, user_id=user_id)[0][0]
    except IndexError:
        raise not_found(f"Project {project}")
    # TODO: Deal with organisation IDs

    query = session.query(
        LogEvent.id,
        func.count(LogEvent.id).over().label("count"),
    ).where(LogEvent.project_id == project_obj.id)
    query = query.order_by(LogEvent.created_at)
    if filter_expr:
        filter_dict = str_filter_exp_to_dict(filter_expr)
        if filter_dict:
            condition = build_filter(filter_dict, LogEvent, session)
            query = query.filter(condition)

    if limit:
        query = query.limit(limit)
    if offset:
        query = query.offset(offset)

    relevant_logs = query.subquery()

    query = (
        session.query(
            Log,
            LogEvent.created_at.label("log_event_ts"),
            relevant_logs.c.count,
        )
        .join(
            LogEvent,
            LogEvent.id == Log.log_event_id,
        )
        .join(
            relevant_logs,
            relevant_logs.c.id == LogEvent.id,
        )
        .where(Log.log_event_id.in_(select(relevant_logs.c.id)))
        .order_by(Log.updated_at)
    )
    all_logs = query.all()
    return all_logs[-1][0].updated_at.isoformat()


@router.get(
    "/logs/metric/{metric}/{key}",
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
    key: str = Path(
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
    log_ids: Optional[List[int]] = Query(
        None,
        description="Log ids to include in the reduction operation. "
        "If none, then all logs are included in the search "
        "(before the filtering is applied).",
        example=[1, 2, 3],
    ),
    project_dao: ProjectDAO = Depends(),
    session=Depends(get_db_session),
) -> Union[float, int, bool]:
    """
    Returns the reduction metric for filtered values for a specific key from a project.
    """
    try:
        user_id = request_fastapi.state.user_id
        project_obj = project_dao.filter(name=project, user_id=user_id)[0][0]
    except IndexError:
        raise not_found(f"Project {project}")
    # TODO: Deal with organisation IDs

    query = session.query(LogEvent.id).filter(LogEvent.project_id == project_obj.id)

    if log_ids:
        query = query.filter(LogEvent.id.in_(log_ids))

    if filter_expr:
        filter_dict = str_filter_exp_to_dict(filter_expr)
        if filter_dict:
            condition = build_filter(filter_dict, LogEvent, session)
            query = query.filter(condition)

    subquery = query.subquery()

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

    reduced_query = (
        session.query(
            reduction_methods[metric](
                case(
                    (
                        Log.inferred_type == "list",
                        func.jsonb_array_length(cast(Log.value, JSONB)).cast(Float),
                    ),
                    (
                        Log.inferred_type == "dict",
                        select(func.count())
                        .select_from(func.jsonb_object_keys(cast(Log.value, JSONB)))
                        .scalar_subquery()
                        .cast(Float),
                    ),
                    (
                        Log.inferred_type == "bool",
                        Log.value.cast(BOOLEAN).cast(INTEGER).cast(Float),
                    ),
                    (
                        Log.inferred_type == "str",
                        func.length(cast(Log.value, JSONB)[0].astext).cast(Float),
                    ),
                    (Log.inferred_type == "float", Log.value.cast(Float)),
                    (Log.inferred_type == "int", Log.value.cast(Float)),
                    else_=0,
                ),
            ),
        )
        .where(Log.key == key)
        .filter(Log.log_event_id.in_(select(subquery)))
    ).scalar()
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
    return {k: json.loads(next(iter(v))) for k, v in groups.items()}


@router.get(
    "/logs/fields",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {
                        "entries": {
                            "col1": "string",
                            "col2": "float",
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
def get_log_fields(
    request_fastapi: Request,
    project: str = Query(
        description="Name of the project to get fields for.",
        example="eval-project",
    ),
    project_dao: ProjectDAO = Depends(),
    session=Depends(get_db_session),
):
    """
    Returns a mapping of fields and their datatypes from a project.
    """
    try:
        user_id = request_fastapi.state.user_id
        project_obj = project_dao.filter(name=project, user_id=user_id)[0][0]
    except IndexError:
        raise not_found(f"Project {project}")

    query = session.query(LogEvent.id).where(LogEvent.project_id == project_obj.id)
    query = query.order_by(desc(LogEvent.created_at))
    query = query.limit(1)

    relevant_logs = query.subquery()

    query = (
        session.query(Log, LogEvent.created_at.label("log_event_ts"))
        .join(
            LogEvent,
            LogEvent.id == Log.log_event_id,
        )
        .where(Log.log_event_id.in_(select(relevant_logs)))
        .order_by(Log.created_at)
    )

    all_logs = query.all()
    formatted_logs, _ = format_logs(all_logs)

    fields = dict()
    for log_dict in formatted_logs.values():
        log = {
            "entries": {
                k: v
                for k, v in log_dict["entries"].items()
                if log_dict["versions"][k] is None
            },
            "params": {
                k: str(log_dict["versions"][k])
                for k, _ in log_dict["entries"].items()
                if log_dict["versions"][k] is not None
            },
        }
        items = {
            "entries": list(log["entries"].items()),
            "params": list(log.get("params", {}).items()),
        }
        fields = {
            key: {item[0]: type(item[1]).__name__ for item in items[key]}
            for key in items
        }
    return fields


@router.get("/logs/field_typing")
def get_field_typing(
    request_fastapi: Request,
    project: str = Query(
        description="Name of the project to get field types for.",
        example="eval-project",
    ),
    project_dao: ProjectDAO = Depends(),
    field_type_dao: FieldTypeDAO = Depends(),
):
    """
    Returns the current typing for each field in the project.
    """
    try:
        user_id = request_fastapi.state.user_id
        project_obj = project_dao.filter(name=project, user_id=user_id)[0][0]
    except IndexError:
        raise not_found(f"Project {project}")

    return field_type_dao.get_field_types(project_obj.id)


@router.post("/logs/field_typing")
def set_field_typing(
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
            existing_types = {type(json.loads(log[0].value)) for log in existing_logs}
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
                    json.loads(existing_logs[0][0].value),
                )
            else:
                # Create a new field type if it does not exist
                field_type_dao.create_field_type(
                    project_id,
                    field_name,
                    json.loads(existing_logs[0][0].value),
                )

        else:  # If we want to turn typing off
            field_type_dao.delete_field_type(project_id, field_name)

    return {"info": "Field typing updated successfully!"}
