"""
Includes endpoints related to entries.
"""

import json
from typing import Any, Dict, Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from sqlalchemy import INTEGER, Float, case, cast, func, select
from sqlalchemy.dialects.postgresql import BOOLEAN, JSONB

from orchestra.db.dao.log_dao import LogDAO, OverwriteError
from orchestra.db.dao.log_event_dao import LogEventDAO
from orchestra.db.dao.project_dao import ProjectDAO
from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import Log, LogEvent
from orchestra.web.api.log.schema import (
    CreateLogConfig,
    DeleteLogEntryRequest,
    DeleteLogsRequest,
    UpdateLogConfig,
    UpdateLogRequest,
)
from orchestra.web.api.utils.http_responses import not_found

from .helpers import build_filter, format_logs, str_filter_exp_to_dict

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

    explicit_types = request.entries.pop("explicit_types", None)

    # Store each key, value pair for the log
    for k, v in request.entries.items():
        try:
            log_dao.create_from_raw_k_v(
                project_id=project_id,
                log_event_id=log_event_id,
                raw_k=k,
                raw_v=v,
                explicit_types=explicit_types,
            )
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="Found different value for log entries with same version.",
            )
    return log_event_id


@router.delete(
    "/log/{id}",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {"info": "Log deleted successfully!"},
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
def delete_log(
    request_fastapi: Request,
    id: int = Path(
        description="ID of the log to delete from a project.",
        example="123",
    ),
    log_event_dao: LogEventDAO = Depends(),
):
    """
    Deletes a log from a project.
    """
    try:
        if log_event_dao.get_user_id(id=id) != request_fastapi.state.user_id:
            raise IndexError
    except IndexError:
        raise not_found(f"Log with id {id}")
    # TODO: Deal with organisation IDs
    log_event_dao.delete(id=id)
    return {"info": "Log deleted successfully!"}


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
    "/log/{id}",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {"info": "Log updated successfully!"},
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
def update_log(
    request_fastapi: Request,
    request: UpdateLogConfig,
    id: int = Path(
        description="ID of the log to update.",
        example="123",
    ),
    log_dao: LogDAO = Depends(),
    log_event_dao: LogEventDAO = Depends(),
):
    """
    Updates the given log with more data.

    A "explicit_types" dictionary can be passed as part of the `entries`.
    If present, any matching key inside this dictionary will override the
    inferred type of that particular entry.

    """

    project_user_id, project_id = log_event_dao.get_user_and_project_id(id=id)

    if project_user_id != request_fastapi.state.user_id:
        raise not_found(f"Log with id {id}")

    explicit_types = request.entries.pop("explicit_types", None)

    # Store each key, value pair for the log
    for k, v in request.entries.items():
        try:
            log_dao.update_value(
                log_event_id=id,
                raw_k=k,
                raw_v=v,
                explicit_types=explicit_types,
                overwrite=request.overwrite,
            )
        except IndexError:
            log_dao.create_from_raw_k_v(
                project_id=project_id,
                log_event_id=id,
                raw_k=k,
                raw_v=v,
                explicit_types=explicit_types,
            )
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="Found different value for log entries with same version.",
            )
        except OverwriteError:
            raise HTTPException(
                status_code=400,
                detail=f"Found existing value for log entry with key {k} but overwrite is set to False.",
            )
    return id


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
):
    """
    Updates multiple logs with the provided entries. Each entry will be either added
    or overridden in the specified logs.

    A dictionary of "explicit_types" can be passed as part of the `entries`.
    If present, it will override the inferred type of any matching key in all logs.
    """
    explicit_types = body.entries.pop("explicit_types", None)
    not_found_logs = []

    for log_id in body.ids:
        try:
            # Get user and project ID for the log
            project_user_id, project_id = log_event_dao.get_user_and_project_id(
                id=log_id,
            )

            # Check if the log belongs to the requesting user
            if project_user_id != request_fastapi.state.user_id:
                raise IndexError

            # Store each key, value pair for the log
            for k, v in body.entries.items():
                try:
                    log_dao.update_value(
                        log_event_id=log_id,
                        raw_k=k,
                        raw_v=v,
                        explicit_types=explicit_types,
                        overwrite=body.overwrite,
                    )
                except IndexError:
                    log_dao.create_from_raw_k_v(
                        project_id=project_id,
                        log_event_id=log_id,
                        raw_k=k,
                        raw_v=v,
                        explicit_types=explicit_types,
                    )
                except ValueError:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Found different value for log entries with the same key '{k}' but a different version.",
                    )
                except OverwriteError:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Found existing value for log entry with key {k} but overwrite is set to False.",
                    )

        except IndexError:
            not_found_logs.append(log_id)

    if not_found_logs:
        raise HTTPException(
            status_code=404,
            detail=f"Logs with ids {not_found_logs} not found or you don't have permission to update them.",
        )

    return {"info": "Logs updated successfully!"}


@router.delete(
    "/log/{id}/entry/{entry}",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {"info": "Log entry deleted successfully!"},
                },
            },
        },
        404_1: {
            "description": "Log Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Log with <id> not found.",
                    },
                },
            },
        },
        404_2: {
            "description": "Log Entry Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Log entry <entry> not found.",
                    },
                },
            },
        },
    },
)
def delete_log_entry(
    request_fastapi: Request,
    id: int = Path(
        description="ID of the log to delete an entry from.",
        example="123",
    ),
    entry: str = Path(
        description="Name of the entry to delete from a given log.",
        example="input-str",
    ),
    log_event_dao: LogEventDAO = Depends(),
    log_dao: LogDAO = Depends(),
):
    """
    Deletes a entry from a log.
    """
    # "/" is replaced with "-" on client side, such that url is parsable
    entry = entry.replace("-", "/")
    if "/" in entry:
        clean_key = entry.split("/")[0]
        version = entry.split("/")[1]
    else:
        clean_key = entry
        version = None
    if log_event_dao.get_user_id(id=id) != request_fastapi.state.user_id:
        raise not_found(f"Log with id {id}")
    # TODO: Deal with organisation IDs
    log = log_dao.filter(log_event_id=id, key=clean_key, version=version)
    if not log:
        raise not_found(f"Log entry {entry}")
    log_dao.delete(id=log[0][0].id)
    return {"info": "Log entry deleted successfully!"}


@router.delete(
    "/logs/entry/{entry}",
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
def delete_log_entry_from_multiple_logs(
    request_fastapi: Request,
    body: DeleteLogEntryRequest,
    entry: str = Path(
        description="Name of the entry to delete from the given logs.",
        example="entry-v0",
    ),
    log_event_dao: LogEventDAO = Depends(),
    log_dao: LogDAO = Depends(),
):
    """
    Deletes a specific entry from multiple logs.
    """
    # Replace "-" with "/" in the entry to handle the client-side encoding
    entry = entry.replace("-", "/")
    if "/" in entry:
        clean_key = entry.split("/")[0]
        version = entry.split("/")[1]
    else:
        clean_key = entry
        version = None

    not_found_logs = []
    not_found_entries = []

    for log_id in body.ids:
        # Verify if the log belongs to the user
        try:
            if log_event_dao.get_user_id(id=log_id) != request_fastapi.state.user_id:
                raise IndexError
        except IndexError:
            not_found_logs.append(log_id)
            continue

        # Check for the existence of the log entry
        log = log_dao.filter(log_event_id=log_id, key=clean_key, version=version)
        if not log:
            not_found_entries.append(log_id)
            continue

        # Delete the log entry
        log_dao.delete(id=log[0][0].id)

    # Handle cases where some logs or entries were not found
    if not_found_logs:
        raise HTTPException(
            status_code=404,
            detail=f"Logs with ids {not_found_logs} not found or you don't have permission to delete from them.",
        )

    if not_found_entries:
        raise HTTPException(
            status_code=404,
            detail=f"Log entry '{entry}' not found in logs with ids {not_found_entries}.",
        )

    return {"info": "Log entry deleted successfully from all logs!"}


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
    entries = {
        l[0].key
        + (f"/{l[0].version}" if l[0].version is not None else ""): json.loads(
            l[0].value,
        )
        for l in log_entries
    }
    return {"id": id, "ts": ts, "entries": entries}


@router.get(
    "/logs",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": [
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
    Returns a list of filtered entries from a project.
    """
    try:
        user_id = request_fastapi.state.user_id
        project_obj = project_dao.filter(name=project, user_id=user_id)[0][0]
    except IndexError:
        raise not_found(f"Project {project}")
    # TODO: Deal with organisation IDs

    query = session.query(LogEvent.id).where(LogEvent.project_id == project_obj.id)
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
        session.query(Log, LogEvent.created_at.label("log_event_ts"))
        .join(
            LogEvent,
            LogEvent.id == Log.log_event_id,
        )
        .where(Log.log_event_id.in_(select(relevant_logs)))
        .order_by(Log.created_at)
    )

    all_logs = query.all()
    formatted_logs = format_logs(all_logs)

    params = dict()
    logs = []
    for log_event_id, log_dict in formatted_logs.items():
        log_dict = formatted_logs[log_event_id]

        for k, v in log_dict["entries"].items():
            if "/" in k:
                _key, _version = k.split("/")
                if _key not in params:
                    params[_key] = dict()
                params[_key][_version] = v

        logs.append(
            {
                "id": log_event_id,
                "ts": log_dict["ts"],
                "entries": {
                    k: v for k, v in log_dict["entries"].items() if "/" not in k
                },
                "params": {
                    k.split("/")[0]: k.split("/")[1]
                    for k, v in log_dict["entries"].items()
                    if "/" in k
                },
            },
        )

    return {
        "params": params,
        "logs": logs,
    }


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
