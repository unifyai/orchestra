"""
Includes endpoints related to logging.
"""

import math
from typing import Literal, Optional, Union

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.param_functions import Depends
from sqlalchemy.orm import Session

from orchestra.db.dao.custom_endpoint_dao import CustomEndpointDAO
from orchestra.db.dao.endpoint_dao import EndpointDAO
from orchestra.db.dao.local_endpoint_dao import LocalEndpointDAO
from orchestra.db.dao.query_dao import QueryDAO
from orchestra.db.dao.tag_dao import TagDAO
from orchestra.db.dependencies import get_db_session
from orchestra.web.api.utils.http_responses import not_found

router = APIRouter()


@router.get("/tags")
def get_query_tags(
    request_fastapi: Request,
    session: Session = Depends(get_db_session),
) -> list[str]:
    tag_dao = TagDAO(session)
    """Returns a list of the tags in your account"""
    return tag_dao.get_all_tags(request_fastapi.state.user_id)


@router.get("/queries")
def get_queries(
    request_fastapi: Request,
    tags: Union[None, str, list[str]] = Query(
        default=None,
        description="Tags to filter for queries that are marked with these tags.",
        example="my_tag",
    ),
    endpoints: Union[None, str, list[str]] = Query(
        default=None,
        description="Optionally specify an endpoint, or a list of endpoints to filter for",
        example="gpt-4o@openai",
    ),
    start_time: Optional[str] = Query(
        None,
        description="Timestamp of the earliest query to aggregate. "
        "Format is `YYYY-MM-DD hh:mm:ss`.",
        example="2024-07-12 04:20:32",
    ),
    end_time: Optional[str] = Query(
        None,
        description="Timestamp of the latest query to aggregate. "
        "Format is `YYYY-MM-DD hh:mm:ss`.",
        example="2024-08-12 04:20:32",
    ),
    page_number: Optional[int] = Query(
        1,
        description="The query history is returned in pages, with up to 20 prompts per page. Increase the page number to see older prompts.",
        example="1",
    ),
    failures: Union[bool, Literal["only"]] = Query(
        False,
        description="indicates whether to includes failures in the return (when set as True ), or whether to return failures exlusively (when set as 'only').",
        example=False,
    ),
    session=Depends(get_db_session),
):
    query_dao = QueryDAO(session)
    endpoint_dao = EndpointDAO(session)
    custom_endpoint_dao = CustomEndpointDAO(session)
    local_endpoint_dao = LocalEndpointDAO(session)
    """
    Get the queries history, optionally for a given set of tags for a narrowed search.
    """
    if tags and isinstance(tags, str):
        tags = [tags]
    if endpoints and isinstance(endpoints, str):
        endpoints = [endpoints]

    global_endpoint_ids = []
    custom_endpoint_ids = []
    local_endpoint_ids = []
    if endpoints:
        for e_str in endpoints:
            try:
                _model, _provider = e_str.split("@")
                if _provider == "external":
                    id_ = local_endpoint_dao.filter(
                        user_id=request_fastapi.state.user_id,
                        name=_model,
                    )[0].id
                    local_endpoint_ids.append(id_)
                elif _provider == "custom":
                    _id = custom_endpoint_dao.filter(
                        user_id=request_fastapi.state.user_id,
                        name=_model,
                    )[0].id
                    custom_endpoint_ids.append(_id)
                else:
                    _id = endpoint_dao.get_endpoints_of(
                        models=[_model],
                        only_from=[_provider],
                    )[0][0].id
                    global_endpoint_ids.append(_id)
            except Exception as e:
                print(e)
                raise not_found(f"Endpoint")

    LIMIT = 20
    if page_number < 1:
        raise HTTPException(
            status_code=400,
            detail=f"Page number: {page_number} must be at least 1.",
        )
    offset = (page_number - 1) * LIMIT
    ret, count = query_dao.filter(
        user_id=request_fastapi.state.user_id,
        tags=tags,
        endpoint_ids=global_endpoint_ids,
        custom_endpoint_ids=custom_endpoint_ids,
        local_endpoint_ids=local_endpoint_ids,
        start_time=start_time,
        end_time=end_time,
        limit=LIMIT,
        offset=offset,
        status_code=200 if failures == False else (400 if failures == "only" else None),
    )
    return {"queries": ret, "total_pages": math.ceil(count / LIMIT)} if ret else ret
