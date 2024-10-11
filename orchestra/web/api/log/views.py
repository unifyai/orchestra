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

from .helpers import (
    evaluate_filter_expression,
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
                        "detail": "A project with this name doesn't exists.",
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

    This method returns the id of the new stored log.
    """
    # check if the project exists
    try:
        project_id = project_dao.filter(
            user_id=request_fastapi.state.user_id,
            # TODO: Add organization id
            name=request.project,
        )[0][0].id
    except IndexError:
        raise HTTPException(
            status_code=404,
            detail="A project with this name doesn't exists.",
        )

    # Create log_event and get its id
    log_event_id = log_event_dao.create(project_id=project_id)

    # Store each key, value pair for the log
    for k, v in request.entries.items():
        inferred_type = type(v).__name__
        clean_key = k.split("/", 1)
        json_v = json.dumps(v)
        log_dao.create(
            log_event_id=log_event_id,
            key=clean_key[0],
            value=json_v,
            version=clean_key[1] if len(clean_key) > 1 else None,
            inferred_type=inferred_type,
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
                        "detail": "Log with id <id> not found in your account.",
                    },
                },
            },
        },
    },
)
def delete_log(
    request_fastapi: Request,
    id: str = Path(
        description="ID of the log to delete from a project.",
        example="123",
    ),
    project_dao: ProjectDAO = Depends(),
    log_event_dao: LogEventDAO = Depends(),
):
    """
    Deletes a log from a project.
    """
    try:
        log_event_project = log_event_dao.filter(id=id)[0][0].project_id
        project_user = project_dao.filter(id=log_event_project)[0][0].user_id
        if request_fastapi.state.user_id != project_user:
            raise IndexError
    except IndexError:
        raise HTTPException(
            status_code=404,
            detail=f"Log with id {id} not found in your account.",
        )
    # TODO: Deal with organisation IDs
    log_event_dao.delete(id=id)
    return {"info": "Log deleted successfully!"}


@router.post(
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
                        "detail": "Log with id <id> not found in your account.",
                    },
                },
            },
        },
    },
)
def update_log(
    request_fastapi: Request,
    request: UpdateLogConfig,
    id: str = Path(
        description="ID of the log to update.",
        example="123",
    ),
    log_dao: LogDAO = Depends(),
    log_event_dao: LogEventDAO = Depends(),
    project_dao: ProjectDAO = Depends(),
):
    """
    Updates the given log with more data.
    """
    log_event_id = int(id)
    log_events = log_event_dao.filter(id=log_event_id)
    if not log_events:
        raise HTTPException(
            status_code=404,
            detail="A log with the specified id does not exist.",
        )
    projects = project_dao.filter(id=log_events[0][0].project_id)
    if not projects or projects[0][0].user_id != request_fastapi.state.user_id:
        raise HTTPException(
            status_code=404,
            detail="A log with the specified id does not exist.",
        )
    # Store each key, value pair for the log
    for k, v in request.entries.items():
        inferred_type = type(v).__name__
        clean_key = k.split("/", 1)
        json_v = json.dumps(v)
        log_dao.create(
            log_event_id=log_event_id,
            key=clean_key[0],
            value=json_v,
            version=clean_key[1] if len(clean_key) > 1 else None,
            inferred_type=inferred_type,
        )
    return log_event_id


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
        404: {
            "description": "Log Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Log with <id> not found in your account.",
                    },
                },
            },
        },
        404: {
            "description": "Log Entry Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Log entry <entry> not found in your account for log <id>.",
                    },
                },
            },
        },
    },
)
def delete_log_entry(
    request_fastapi: Request,
    id: str = Path(
        description="ID of the log to delete an entry from.",
        example="123",
    ),
    entry: str = Path(
        description="Name of the entry to delete from a given log.",
        example="input-str",
    ),
    project_dao: ProjectDAO = Depends(),
    log_event_dao: LogEventDAO = Depends(),
    log_dao: LogDAO = Depends(),
):
    """
    Deletes a entry from a log.
    """
    try:
        log_event = log_event_dao.filter(id=id)[0][0]
        project = project_dao.filter(id=log_event.project_id)[0][0]
        if request_fastapi.state.user_id != project.user_id:
            raise IndexError
    except IndexError:
        raise HTTPException(
            status_code=404,
            detail=f"Log with id {id} not found in your account.",
        )
        # TODO: Deal with organisation IDs
    log = log_dao.filter(log_event_id=log_event.id, key=entry)
    if not log:
        raise HTTPException(
            status_code=404,
            detail=f"Log entry {entry} not found in your account for log {id}.",
        )
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
                        "detail": "Log with id <id> not found in your account.",
                    },
                },
            },
        },
    },
)
def get_log(
    request_fastapi: Request,
    id: str = Path(
        description="ID of the log to fetch.",
        example="123",
    ),
    log_event_dao: LogEventDAO = Depends(),
    project_dao: ProjectDAO = Depends(),
    log_dao: LogDAO = Depends(),
):
    """
    Returns the log associated with a given id.
    """
    try:
        log_event = log_event_dao.filter(id=id)[0][0]
        project = project_dao.filter(id=log_event.project_id)[0][0]
        if request_fastapi.state.user_id != project.user_id:
            raise IndexError
    except IndexError:
        raise HTTPException(
            status_code=404,
            detail=f"Log with id {id} not found in your account.",
        )
    # TODO: Deal with organisation IDs
    log_entries = log_dao.filter(log_event_id=log_event.id)
    entries = {l[0].key: json.loads(l[0].value) for l in log_entries}
    return {"id": id, "entries": entries}


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
                            "entries": {
                                "key1": "a",
                                "key2": 1.0,
                            },
                        },
                        {
                            "id": "1",
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
                        "detail": "Project <project> not found in your account.",
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
    log_event_dao: LogEventDAO = Depends(),
    project_dao: ProjectDAO = Depends(),
    log_dao: LogDAO = Depends(),
):
    """
    Returns a list of filtered entries from a project.
    """
    try:
        project_obj = project_dao.filter(name=project)[0][0]
        if request_fastapi.state.user_id != project_obj.user_id:
            raise IndexError
    except IndexError:
        raise HTTPException(
            status_code=404,
            detail=f"Project {project} not found in your account.",
        )
    # TODO: Deal with organisation IDs
    log_events = log_event_dao.filter(project_id=project_obj.id)
    logs = []
    for le in log_events:
        # TODO: This is super slow
        # TODO: Add pagination
        log_entries = log_dao.filter(log_event_id=le[0].id)
        entries = {l[0].key: json.loads(l[0].value) for l in log_entries}
        if filter_expr is None or evaluate_filter_expression(
            str_filter_exp_to_dict(filter_expr), **entries
        ):
            logs.append({"id": le[0].id, "entries": entries})
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
                        "detail": "Project <project> not found in your account.",
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
        project_obj = project_dao.filter(name=project)[0][0]
        if request_fastapi.state.user_id != project_obj.user_id:
            raise IndexError
    except IndexError:
        raise HTTPException(
            status_code=404,
            detail=f"Project {project} not found in your account.",
        )
    # TODO: Deal with organisation IDs
    log_events = log_event_dao.filter(project_id=project_obj.id)
    filter_dict = (
        (str_filter_exp_to_dict(filter_expr)) if filter_expr is not None else {}
    )
    # TODO: This is super slow
    # TODO: Add pagination
    log_entries = [
        json.loads(log_dao.filter(log_event_id=e[0].id, key=key)[0][0].value)
        for e in log_events
    ]
    log_entries = [
        e
        for e in log_entries
        if ((not filter_dict) or evaluate_filter_expression(filter_dict, **{key: e}))
    ]
    if not log_entries:
        raise Exception(
            "No values remaining after applying filtering, "
            "cannot compute reduction metric",
        )
    return reduction_methods[metric](log_entries)


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
                        "detail": "Project <project> not found in your account.",
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
        project_obj = project_dao.filter(name=project)[0][0]
        if request_fastapi.state.user_id != project_obj.user_id:
            raise IndexError
    except IndexError:
        raise HTTPException(
            status_code=404,
            detail=f"Project {project} not found in your account.",
        )
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
