"""
Includes endpoints related to entries.
"""

import json
from typing import Any, Dict, Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request

from orchestra.db.dao.log_dao import LogDAO
from orchestra.db.dao.log_event_dao import LogEventDAO
from orchestra.db.dao.project_dao import ProjectDAO
from orchestra.web.api.log.schema import CreateLogConfig, UpdateLogConfig
from orchestra.web.api.utils.http_responses import not_found

from .helpers import (
    KeyNotFound,
    evaluate_filter_expression,
    format_logs,
    reduction_methods,
    str_filter_exp_to_dict,
)

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
    return id


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
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
    log_event_dao: LogEventDAO = Depends(),
    project_dao: ProjectDAO = Depends(),
    log_dao: LogDAO = Depends(),
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
    log_events = log_event_dao.filter(
        project_id=project_obj.id,
        limit=limit,
        offset=offset,
    )
    all_logs = log_dao.filter(log_event_id=[le[0].id for le in log_events])
    formatted_logs = format_logs(all_logs)
    # TODO: Add pagination
    logs = list()
    filter_dict = str_filter_exp_to_dict(filter_expr) if filter_expr is not None else {}
    for log_event_id, log_dict in formatted_logs.items():
        if filter_dict:
            try:
                match = evaluate_filter_expression(filter_dict, **log_dict["entries"])
                if match == False:
                    continue
            except KeyNotFound:
                continue

        logs.append(
            {
                "id": log_event_id,
                "ts": log_dict["ts"],
                "entries": log_dict["entries"],
            },
        )
    return logs


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
    log_event_dao: LogEventDAO = Depends(),
    project_dao: ProjectDAO = Depends(),
    log_dao: LogDAO = Depends(),
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
    log_events = log_event_dao.filter(project_id=project_obj.id)
    filter_dict = (
        (str_filter_exp_to_dict(filter_expr)) if filter_expr is not None else {}
    )
    # format
    all_logs = log_dao.filter(log_event_id=[e[0].id for e in log_events])
    formatted_logs = format_logs(all_logs)
    # filter
    filtered_logs = dict()
    for log_event_id, log_dict in formatted_logs.items():
        if key not in log_dict["entries"]:
            continue
        if filter_dict == {} or evaluate_filter_expression(
            filter_dict, **log_dict["entries"]
        ):
            filtered_logs[log_event_id] = log_dict["entries"]
    # TODO: Add pagination
    if not filtered_logs:
        raise Exception(
            "No values remaining after applying filtering, "
            "cannot compute reduction metric",
        )
    return reduction_methods[metric](
        [dct[key] for log_id, dct in filtered_logs.items()],
    )


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
