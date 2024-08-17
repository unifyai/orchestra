import datetime
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.param_functions import Depends

from orchestra.db.dao.custom_api_key_dao import CustomApiKeyDAO
from orchestra.db.dao.custom_endpoint_benchmark_dao import CustomEndpointBenchmarkDAO
from orchestra.db.dao.custom_endpoint_dao import CustomEndpointDAO
from orchestra.db.models.orchestra_models import CustomEndpoint
from orchestra.web.api.custom_endpoints.schema import CustomEndpointModelResponse
from orchestra.web.api.utils.http_responses import custom_endpoint_not_found

router = APIRouter()


@router.get("/custom_endpoint", response_model=List[CustomEndpointModelResponse])
def get_custom_endpoints(
    request_fastapi: Request,
    custom_endpoint_dao: CustomEndpointDAO = Depends(),
) -> List[CustomEndpoint]:
    """
    Returns a list of the available custom endpoints.
    """
    user_id = request_fastapi.state.user_id
    return custom_endpoint_dao.get_user_endpoints(user_id=user_id)


@router.put(
    "/custom_endpoint",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {"info": "Custom endpoint created succesfully!"},
                },
            },
        },
        404: {
            "description": "Custom API Key Not Found",
            "content": {
                "application/json": {
                    "example": {"detail": "Custom API Key not found."},
                },
            },
        },
    },
)
def create_custom_endpoint(
    request_fastapi: Request,
    name: str = Query(
        ...,
        description="Alias for the custom endpoint. This will be the name used to call the endpoint.",
        example="endpoint1",
    ),
    url: str = Query(
        ...,
        description="Base URL of the endpoint being called. Must support the OpenAI format.",
        example="https://api.url1.com",
    ),
    key_name: str = Query(
        ...,
        description="Name of the API key that will be passed as part of the query.",
        example="key1",
    ),
    mdl_name: Optional[str] = Query(
        None,
        description=(
            "Named passed to the custom endpoint as model name. "
            "If not specified, it will default to the endpoint alias."
        ),
        example="llama-3.1-8b-finetuned",
    ),
    custom_endpoint_dao: CustomEndpointDAO = Depends(),
    custom_api_key_dao: CustomApiKeyDAO = Depends(),
) -> None:
    """
    Creates a custom endpoint. This endpoint must support the OpenAI `/chat/completions`
    format. To query your custom endpoint, replace your endpoint string with `<name>@custom`
    when querying the unified API.

    """
    user_id = request_fastapi.state.user_id
    try:
        key_id = custom_api_key_dao.filter(user_id=user_id, key=key_name)[0].id
    except Exception:
        raise HTTPException(status_code=404, detail="Custom API Key not found.")

    custom_endpoint_dao.create_custom_endpoint(
        user_id=user_id,
        name=name,
        mdl_name=mdl_name if mdl_name else name,
        url=url,
        key_id=key_id,
    )
    return {"info": "Custom endpoint created succesfully!"}


@router.post(
    "/custom_endpoint/rename",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {"info": "Custom endpoint renamed succesfully!"},
                },
            },
        },
        404: {
            "description": "Custom endpoint Not Found",
            "content": {
                "application/json": {
                    "example": {"detail": "Custom endpoint not found."},
                },
            },
        },
    },
)
def rename_custom_endpoint(
    request_fastapi: Request,
    name: str = Query(
        ...,
        description="Name of the custom endpoint to be updated.",
        example="name1",
    ),
    new_name: str = Query(
        ...,
        description="New name for the custom endpoint.",
        example="name2",
    ),
    custom_endpoint_dao: CustomEndpointDAO = Depends(),
) -> None:
    """
    Renames a custom endpoint in your account.

    """
    user_id = request_fastapi.state.user_id

    existing_endpoint = custom_endpoint_dao.filter(user_id=user_id, name=name)
    if not existing_endpoint:
        raise custom_endpoint_not_found

    custom_endpoint_dao.rename(
        user_id=user_id,
        name=name,
        new_name=new_name,
    )
    return {"info": "Custom endpoint renamed succesfully!"}


@router.delete(
    "/custom_endpoint",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {"info": "Custom endpoint deleted succesfully!"},
                },
            },
        },
    },
)
def delete_custom_endpoint(
    request_fastapi: Request,
    name: str = Query(
        ...,
        description="Name of the custom endpoint to delete.",
        example="endpoint1",
    ),
    custom_endpoint_dao: CustomEndpointDAO = Depends(),
) -> None:
    """
    Deletes a custom endpoint in your account.

    """
    user_id = request_fastapi.state.user_id

    existing_endpoint = custom_endpoint_dao.filter(user_id=user_id, name=name)
    if not existing_endpoint:
        raise custom_endpoint_not_found

    custom_endpoint_dao.delete(
        user_id=user_id,
        name=name,
    )
    return {"info": "Custom endpoint deleted succesfully!"}


ALLOWED_METRICS = [
    "input-cost",
    "output-cost",
    "tokens-per-second",
    "time-to-first-token",
    "inter-token-latency",
    "end-2-end-latency",
    "cold-start",
]
ALLOWED_METRICS_STR = ""
for metric in ALLOWED_METRICS:
    ALLOWED_METRICS_STR += f'"{metric}", '
ALLOWED_METRICS_STR = ALLOWED_METRICS_STR[:-2]


@router.post(
    "/custom_endpoint/benchmark",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {
                        "info": "Custom endpoint benchmark uploaded succesfully!",
                    },
                },
            },
        },
        400: {
            "description": "Benchmark not valid",
            "content": {
                "application/json": {
                    "example": {"detail": "Invalid data submitted"},
                },
            },
        },
    },
)
def upload_custom_benchmark(
    request_fastapi: Request,
    endpoint_name: str = Query(
        ...,
        description="Name of the custom endpoint to submit a benchmark for.",
        example="endpoint1",
    ),
    metric_name: str = Query(
        ...,
        description=f"""Name of the metric to submit. Allowed metrics are: {ALLOWED_METRICS_STR}.""",
        example="tokens-per-second",
    ),
    value: float = Query(
        ...,
        description="Value of the metric to submit.",
        example=10,
    ),
    custom_endpoint_dao: CustomEndpointDAO = Depends(),
    custom_endpoint_benchmark_dao: CustomEndpointBenchmarkDAO = Depends(),
):
    if metric_name not in ALLOWED_METRICS:
        raise HTTPException(
            status_code=400,
            detail=f"{metric_name} not one of the allowed metrics. Allowed metrics are: {ALLOWED_METRICS_STR}.",
        )
    # check if the endpoint is valid
    user_id = request_fastapi.state.user_id
    available_endpoints = custom_endpoint_dao.filter(
        user_id=user_id,
        name=endpoint_name,
    )
    for endpoint in available_endpoints:
        if endpoint_name == endpoint.name:
            endpoint_id = endpoint.id
            break
    else:
        raise HTTPException(
            status_code=400,
            detail=f"""The endpoint: {endpoint_name} was not found in your account.""",
        )
    custom_endpoint_benchmark_dao.upload_benchmark(
        endpoint_id=endpoint_id,
        metric_name=metric_name,
        value=value,
        measured_at=datetime.datetime.now(),
    )
    return {"info": "Benchmark uploaded!"}


@router.get(
    "/custom_endpoint/get_benchmark",
)
def get_custom_benchmarks(
    request_fastapi: Request,
    endpoint_name: str = Query(
        ...,
        description="Name of the custom endpoint to get a benchmark for.",
        example="endpoint1",
    ),
    metric_name: str = Query(
        ...,
        description="Name of the metric to get the benchmark of.",
        example="tokens-per-second",
    ),
    start_time: str = Query(
        default="2024-01-01",
        description="Start time of window to get benchmarks between. Format YYYY-MM-DD",
        example="2024-01-01",
    ),
    end_time: str = Query(
        default="2024-12-12",
        description="End time of window to get benchmarks between. Format YYYY-MM-DD",
        example="2024-12-12",
    ),
    custom_endpoint_dao: CustomEndpointDAO = Depends(),
    custom_endpoint_benchmark_dao: CustomEndpointBenchmarkDAO = Depends(),
):
    user_id = request_fastapi.state.user_id
    available_endpoints = custom_endpoint_dao.filter(
        user_id=user_id,
        name=endpoint_name,
    )
    for endpoint in available_endpoints:
        if endpoint_name == endpoint.name:
            endpoint_id = endpoint.id
            break
    else:
        raise HTTPException(
            status_code=400,
            detail=f"""The endpoint: {endpoint_name} was not found in your account.""",
        )

    ret = custom_endpoint_benchmark_dao.benchmarks_between(
        endpoint_id=endpoint_id,
        metric_name=metric_name,
        start_time=start_time,
        end_time=end_time,
    )
    return ret
