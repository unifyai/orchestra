"""
Includes endpoints related to logging.
"""

import os
from typing import Any, Dict, List, Union, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.param_functions import Depends

from orchestra.db.dao.query_dao import QueryDAO
from orchestra.db.dao.tag_dao import TagDAO
from orchestra.db.dao.endpoint_dao import EndpointDAO
from orchestra.db.dao.custom_endpoint_dao import CustomEndpointDAO
from orchestra.db.dao.local_endpoint_dao import LocalEndpointDAO
from orchestra.web.api.utils.on_prem import handle_on_prem

router = APIRouter()

@router.get("/tags")
def get_query_tags(
    request_fastapi: Request,
    tag_dao: TagDAO = Depends(),
) -> list[str]:
    """Returns a list of the tags in your account"""
    return tag_dao.get_all_tags(request_fastapi.state.user_id)

@router.get("/queries")
def get_query_history(
    request_fastapi: Request,
    tags: Union[None, str, list[str]] = Query(
        default=None,
        description="Tags to filter for queries that are marked with these tags.",
        example="my_tag",
    ),
    models: Union[None, str, list[str]] = Query(
        default=None,
        description="Optionally specify a model or list of models to filter for",
        example="gpt-4o",
    ),
    providers: Union[None, str, list[str]] = Query(
        default=None,
        description="Optionally specify a provider or list of providers to filter for",
        example="openai",
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
    query_dao: QueryDAO = Depends(),
    endpoint_dao: EndpointDAO = Depends(),
    custom_endpoint_dao: CustomEndpointDAO = Depends(),
    local_endpoint_dao: LocalEndpointDAO = Depends(),
):
    """
    Get the queries history, optionally for a given set of tags for a narrowed search.
    """
    if tags and isinstance(tags, str):
        tags = list(tags)
    if models or providers or start_time or end_time:
        raise HTTPException(status_code=501, detail="Not implemented yet")
    ## filter
    # logic to get a list of endpoints, custom_endpoints, local_endpoints
    endpoints = []
    custom_endpoints = []
    local_endpoints = []
    # need logic for
    # standard models
    # model@provider
    
    # custom endpoints
    # @custom
    # things that don't even go through orchestra
    # @external
    
    ret = query_dao.filter(
        user_id=request_fastapi.state.user_id,
        tags=tags,
        # models=models,
        # providers=providers,
        # start_time=start_time,
        # end_time=end_time,
    )
    return ret


@router.get("/metrics")
@handle_on_prem(endpoint="/metrics", method="none")
def get_query_metrics(
    request_fastapi: Request,
    start_time: str = Query(
        None,
        description="Timestamp of the earliest query to aggregate. "
        "Format is `YYYY-MM-DD hh:mm:ss`.",
        example="2024-07-12 04:20:32",
    ),
    end_time: str = Query(
        None,
        description="Timestamp of the latest query to aggregate. "
        "Format is `YYYY-MM-DD hh:mm:ss`.",
        example="2024-08-12 04:20:32",
    ),
    models: str = Query(
        None,
        description=(
            "Models to fetch metrics from. "
            "The list must be a set of comma-separated strings. "
            "i.e. `gpt-3.5-turbo,gpt-4o`"
        ),
        example="gpt-4o,llama-3.1-405b-chat,claude-3.5-sonnet",
    ),
    providers: str = Query(
        None,
        description=(
            "Providers to fetch metrics from. "
            "The list must be a set of comma-separated strings. "
            "i.e. `openai,together-ai`"
        ),
        example="openai,anthropic,fireworks-ai",
    ),
    interval: str = Query(
        300,
        description="Number of seconds in the aggregation interval.",
        example=300,
    ),
    secondary_user_id: str = Query(
        None,
        description=(
            "Secondary user id. The secondary user id will match any string "
            "previously sent in the `user` attribute of `/chat/completions`."
        ),
        example="sample_user_id",
    ),
) -> Dict[str, Any]:
    """
    Returns aggregated telemetry data from previous queries to the `/chat/completions`
    endpoint, specifically the p50 and p95 for generation time and tokens per second,
    and also the total prompt and completion tokens processed within the interval. The
    user id and total request count within the interval are also returned.
    """
    import requests

    if secondary_user_id is None:
        secondary_user_id = ""

    response = requests.get(
        "https://api.airfold.co/v1/pipes/queries_metrics.json",
        # TODO: mb will rotate this tomorrow
        headers={
            "Authorization": f"Bearer {os.environ.get('AIRFOLD_KEY')}",
        },
        params={
            "user_id": request_fastapi.state.user_id,
            "secondary_user_id": secondary_user_id,
            "start_time": start_time,
            "end_time": end_time,
            "models": models,
            "providers": providers,
            "interval": interval,
        },
    )

    if response.status_code == 200:
        data = response.json()
        return data
    else:
        # TODO: meaningful errors
        print("Error:", response.status_code, response.text)
