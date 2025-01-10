"""
Includes endpoints related to entries.
"""

import json
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from sqlalchemy import INTEGER, TIMESTAMP, Float, case, cast, func, select
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
    SetFieldTypingRequest,
    UpdateLogRequest,
)
from orchestra.web.api.utils.http_responses import not_found

from .helpers import (
    STR_TO_SQL_TYPES,
    _flatten_fields,
    build_filter,
    format_logs,
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
    session,
    latest_timestamp=False,
):
    # try to get the project, and fail if not found
    try:
        user_id = request_fastapi.state.user_id
        project_obj = project_dao.filter(name=project, user_id=user_id)[0][0]
    except IndexError:
        raise not_found(f"Project {project}")

    # filter for log event ids within the project
    log_event_query = session.query(
        LogEvent.id,
    ).where(LogEvent.project_id == project_obj.id)

    # remove irrelevant log event ids based on from_ids and exclude_ids
    assert not (from_ids and exclude_ids), (
        f"Only one of from_ids or exclude_ids can be set, "
        f"but found values {from_ids} and {exclude_ids}."
    )
    if from_ids:
        log_event_query = log_event_query.where(
            LogEvent.id.in_([int(i) for i in from_ids.split("&")]),
        )
    elif exclude_ids:
        log_event_query = log_event_query.where(
            LogEvent.id.notin_([int(i) for i in exclude_ids.split("&")]),
        )

    # filter the log event ids based on the values of their fields (filter argument)
    if filter_expr:
        filter_dict = str_filter_exp_to_dict(filter_expr)
        if filter_dict:
            condition = build_filter(filter_dict, LogEvent, session)
            log_event_query = log_event_query.filter(condition)

    # create sub-query for these relevant log events
    relevant_log_events = log_event_query.subquery()

    # query the logs themselves, which match the log event ids
    log_query = (
        session.query(
            Log,
            LogEvent.created_at,
            LogEvent.id,
        )
        .join(
            LogEvent,
            LogEvent.id == Log.log_event_id,
        )
        .join(
            relevant_log_events,
            relevant_log_events.c.id == LogEvent.id,
        )
    )

    # filter out all logs which are not within the context
    context_len = 0
    if context is not None:
        split_context = context.split("/")
        exclude_params = "entries" in split_context
        exclude_entries = "params" in split_context
        assert not (
            exclude_params and exclude_entries
        ), "'entries' and 'params' cannot both be specified in the context argument."
        context = "/".join(
            [substr for substr in split_context if substr not in ("params", "entries")],
        )
        if context:
            context = context if context[-1] == "/" else context + "/"
            context_len = len(context)
            log_query = log_query.where(Log.key.startswith(context))
        if exclude_params:
            log_query = log_query.where(Log.version.is_(None))
        elif exclude_entries:
            log_query = log_query.where(Log.version.isnot(None))

    # filter out all irrelevant logs as per from_fields and exclude_fields
    assert not (from_fields and exclude_fields), (
        f"Only one of from_fields or exclude_fields can be set, "
        f"but found values {from_fields} and {exclude_fields}."
    )
    if from_fields:
        log_query = log_query.where(Log.key.in_(from_fields.split("&")))
    elif exclude_fields:
        log_query = log_query.where(Log.key.notin_(exclude_fields.split("&")))

    # create a sub-query of these relevant logs
    relevant_logs = log_query.subquery()

    # create a second set of relevant log event ids, removing all log events which did
    # not contain any relevant fields as per the context and field pruning

    # query for the distinct log event ids
    distinct_ids_subq = (
        session.query(LogEvent.id)
        .join(Log, Log.log_event_id == LogEvent.id)
        .join(relevant_logs, relevant_logs.c.id == Log.id)
        .distinct()
        .subquery()
    )

    # sort the log ids based on the sorting criteria provided by the user,
    # and dynamically add a post-sorting row number to keep the order info preserved
    sort_criteria = list()
    sorted_query = session.query(distinct_ids_subq.c.id)
    if sorting:
        subqs = {}
        for key in json.loads(sorting):
            subqs[key] = (
                session.query(LogEvent.id, Log.value, Log.inferred_type)
                .join(Log, LogEvent.id == Log.log_event_id)
                .where(Log.key == key)
                .subquery()
            )
            field_types = field_type_dao.get_field_types(project_obj.id)
        for key, sort_mode in json.loads(sorting).items():
            subq = subqs[key]
            # Outer join to bring in the needed columns for sorting
            sorted_query = sorted_query.outerjoin(
                subq,
                subq.c.id == distinct_ids_subq.c.id,
            )

            if key in field_types:
                criterion = cast(subq.c.value, STR_TO_SQL_TYPES[field_types[key]])
            else:
                criterion = subq.c.value

            if sort_mode == "ascending":
                sort_criteria.append(criterion.asc().nulls_last())
            elif sort_mode == "descending":
                sort_criteria.append(criterion.desc().nulls_last())
            else:
                raise HTTPException(
                    status_code=400,
                    detail="sort_mode must be 'ascending' or 'descending', "
                    f"but found {sort_mode}.",
                )
    sort_criteria.append(LogEvent.id.desc())

    log_event_query = (
        sorted_query.join(
            LogEvent,
            LogEvent.id == distinct_ids_subq.c.id,
        )
        .add_columns(
            func.row_number().over(order_by=sort_criteria).label("row_num"),
        )
        .order_by("row_num")
    )

    count = log_event_query.count()
    if limit:
        log_event_query = log_event_query.limit(limit)
    if offset:
        log_event_query = log_event_query.offset(offset)
    relevant_log_events = log_event_query.subquery()

    # the final log query, with all of the pruning, filtering, sorting,
    # and pagination applied
    log_query = log_query.join(
        relevant_log_events,
        relevant_log_events.c.id == LogEvent.id,
    )

    if latest_timestamp:
        relevant_logs = log_query.subquery()
        max_query = session.query(func.max(Log.updated_at)).join(
            relevant_logs,
            relevant_logs.c.id == Log.id,
        )
        return max_query.scalar().isoformat()
    return (
        log_query.order_by(relevant_log_events.c.row_num, Log.created_at).all(),
        context_len,
        count,
    )


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
    context: Optional[str] = Query(
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
    session=Depends(get_db_session),
):
    """
    Returns a list of filtered entries from a project.
    """
    all_logs, context_len, count = _get_logs_query(
        request_fastapi,
        project,
        context,
        filter_expr,
        sorting,
        from_ids,
        exclude_ids,
        from_fields,
        exclude_fields,
        limit,
        offset,
        project_dao,
        field_type_dao,
        session,
    )
    if return_ids_only:
        return list(dict.fromkeys([log[0].log_event_id for log in all_logs]))

    formatted_logs = format_logs(all_logs, context_len)

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
    context: Optional[str] = Query(
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
    session=Depends(get_db_session),
):
    """
    Returns the update timestamp of the most recently updated log within the specified
    page and filter bounds.
    """
    return _get_logs_query(
        request_fastapi,
        project,
        context,
        filter_expr,
        sorting,
        from_ids,
        exclude_ids,
        from_fields,
        exclude_fields,
        limit,
        offset,
        project_dao,
        field_type_dao,
        session,
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
    Returns the reduction metric for filtered values for a specific key from a project.
    """
    try:
        user_id = request_fastapi.state.user_id
        project_obj = project_dao.filter(name=project, user_id=user_id)[0][0]
    except IndexError:
        raise not_found(f"Project {project}")
    # TODO: Deal with organisation IDs

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
                    (
                        Log.inferred_type == "timestamp",
                        func.extract("epoch", cast(Log.value, TIMESTAMP)).cast(Float),
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
    field_type = field_type_dao.get_field_types(project_obj.id).get(key)
    if metric == "count":
        return int(reduced_query)
    elif not field_type:
        return reduced_query
    elif field_type == "timestamp":
        if metric in ("var", "std"):
            return timedelta(seconds=reduced_query).__repr__()
        return datetime.fromtimestamp(reduced_query).isoformat()
    elif (
        reduced_query.is_integer()
        and metric in ("sum", "min", "max", "median", "mode")
        and field_type in ("int", "bool", "str")
    ):
        if field_type == "bool" and metric in ("min", "max", "median", "mode"):
            return bool(reduced_query)
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
    return {k: json.loads(next(iter(v))) for k, v in groups.items()}


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
    all_logs, _, _ = _get_logs_query(
        request_fastapi,
        project,
        None,
        None,
        None,
        None,
        None,
        all_field_names,
        None,
        1,
        0,
        project_dao,
        field_type_dao,
        session,
    )
    field_types = dict(
        (lg[0].key, "entry" if lg[0].version is None else "param") for lg in all_logs
    )
    # end ToDo

    return {
        key: {
            "data_type": types.get(key),
            "field_type": field_type,
        }
        for key, field_type in field_types.items()
    }


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

    return {"info": "Field types updated successfully!"}
