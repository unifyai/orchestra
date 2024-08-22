"""
Includes endpoints related to logging.
"""
import os
from typing import Dict, Any, List, Union
from fastapi.param_functions import Depends
from fastapi import APIRouter, HTTPException, Query, Request

from orchestra.db.dao.query_dao import QueryDAO
from orchestra.web.api.utils.on_prem import handle_on_prem

router = APIRouter()


@router.get("/prompt_history")
def get_prompt_history(
    request_fastapi: Request,
    tags: Union[str, List[str]] = Query(
        default=None,
        description="Tags to filter for prompts that are marked with these tags.",
    ),
    query_dao: QueryDAO = Depends(),
):
    """
    Get the prompt history, optionally for a given set of tags for a narrowed search.
    """
    if tags:
        raise HTTPException(status_code=501, detail="Not Implemented Yet")
    ret = query_dao.filter(user_id=request_fastapi.state.user_id)
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
