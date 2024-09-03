"""
Includes endpoints related to logging.
"""

import os
from typing import Any, Dict, List, Union, Optional
from datetime import datetime

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
    query_dao: QueryDAO = Depends(),
    endpoint_dao: EndpointDAO = Depends(),
    custom_endpoint_dao: CustomEndpointDAO = Depends(),
    local_endpoint_dao: LocalEndpointDAO = Depends(),
):
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
                        user_id=request_fastapi.state.user_id, name=_model
                    )[0].id
                    local_endpoint_ids.append(id_)
                elif _provider == "custom":
                    _id = custom_endpoint_dao.filter(
                        user_id=request_fastapi.state.user_id, name=_model
                    )[0].id
                    custom_endpoint_ids.append(_id)
                else:
                    _id = endpoint_dao.get_endpoints_of(
                        models=[_model], only_from=[_provider]
                    )[0][0].id
                    global_endpoint_ids.append(_id)
            except Exception as e:
                print(e)
                raise HTTPException(
                    status_code=404, detail=f"Could not find endpoint: {e_str}"
                )

    ret = query_dao.filter(
        user_id=request_fastapi.state.user_id,
        tags=tags,
        endpoint_ids=global_endpoint_ids,
        custom_endpoint_ids=custom_endpoint_ids,
        local_endpoint_ids=local_endpoint_ids,
        start_time=start_time,
        end_time=end_time,
    )
    return ret


@router.post("/queries")
def log_query(
    request_fastapi: Request,
    endpoint: str = Query(
        description="Endpoint to log query for.",
        example="llama-3.1-8b-chat_ollama@external",
    ),
    query_body: str = Query(
        description="A string containing the body of the request",
        example="""'{"messages": [{"role": "system", "content": "You are an useful assistant"}, {"role": "user", "content": "Explain who Newton was."}], "model": "llama-3-8b-chat@aws-bedrock", "max_tokens": 100,"stream": false, "temperature": 0.5,}'""",
    ),
    response_body: Optional[str] = Query(
        None,
        description="An optional string containing the response to the request",
        example="""'{"model": "meta.llama3-8b-instruct-v1:0", "created": 1725396241, "id": "chatcmpl-92d3b36e-7b64-4ae8-8102-9b7e3f5dd30f", "object": "chat.completion", "usage": {"completion_tokens": 100, "prompt_tokens": 44, "total_tokens": 144}, "choices": [{"finish_reason": "stop", "index": 0, "message": {"content": "Sir Isaac Newton was an English mathematician, physicist, and astronomer who lived from 1643 to 1727.\\n\\nHe is widely recognized as one of the most influential scientists in history, and his work laid the foundation for the Scientific Revolution of the 17th century.\\n\\nNewton\'s most famous achievement is his theory of universal gravitation, which he presented in his groundbreaking book \\"Philosophi\\u00e6 Naturalis Principia Mathematica\\" in 1687.\\n\\nAccording to Newton\'s theory, every", "role": "assistant", "tool_calls": null, "function_call": null}}]}'""",
    ),
    tags: Optional[list[str]] = Query(None, description="Tags for later filtering."),
    timestamp: Optional[str] = Query(
        None,
        description="A timestamp (if not set, will be the time of sending)",
        example="2024-07-12T04:20:32.808410",
    ),
    query_dao: QueryDAO = Depends(),
    local_endpoint_dao: LocalEndpointDAO = Depends(),
):
    if not timestamp:
        timestamp = str(datetime.now())

    _model_name = endpoint.split("@")[0]
    local_endpoint_id = local_endpoint_dao.get_local_endpoint(
        user_id=request_fastapi.state.user_id, name=_model_name
    )

    try:
        query_dao.create_query(
            user_id=request_fastapi.state.user_id,
            at=timestamp,
            model_provider_str=endpoint,
            endpoint_id=None,
            custom_endpoint_id=None,
            local_endpoint_id=local_endpoint_id,
            credits=0,
            query_body=query_body,
            response_body=response_body,
            tags=tags,
        )
        return {"info": "Query logged successfully"}
    except Exception as e:
        print(e)
        raise HTTPException(status_code=404, detail="Error in logging query")


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
