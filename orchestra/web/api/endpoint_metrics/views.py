"""
Functions for endpoint metrics, such as input cost, output cost, inter-token-latency,
time-to-first-token etc.
"""

import os
from datetime import datetime
from itertools import chain
from typing import Dict, List, Union

import requests
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.param_functions import Depends

from orchestra.db.dao.benchmark_run_dao import BenchmarkRunDAO
from orchestra.db.dao.custom_endpoint_benchmark_dao import CustomEndpointBenchmarkDAO
from orchestra.db.dao.custom_endpoint_dao import CustomEndpointDAO
from orchestra.db.dao.endpoint_dao import EndpointDAO
from orchestra.db.dao.latest_benchmark_dao import LatestBenchmarkDAO
from orchestra.web.api.utils.http_responses import model_not_found, not_found

router = APIRouter()

ALLOWED_METRICS = [
    "input-cost",
    "output-cost",
    "time-to-first-token",
    "inter-token-latency",
]
ALLOWED_METRICS_STR = ""
for metric in ALLOWED_METRICS:
    ALLOWED_METRICS_STR += f'"{metric}", '
ALLOWED_METRICS_STR = ALLOWED_METRICS_STR[:-2]


def _get_endpoint_from_model_provider(
    model: str,
    provider: str,
    endpoint_dao: EndpointDAO,
):
    try:
        endpoints = endpoint_dao.get_endpoints_of(
            models=(model,) if isinstance(model, str) else model,
            only_from=(provider,) if isinstance(provider, str) else provider,
        )
        endpoints = [
            {
                "id": endpoint[0].id,
                "model": endpoint[1].mdl_code,
                "provider": endpoint[2].name,
            }
            for endpoint in endpoints
        ]
        return endpoints
    except:
        raise model_not_found


def _get_custom_endpoint_benchmark(
    request_fastapi: Request,
    model: str,
    start_time: str = None,
    end_time: str = None,
    custom_endpoint_dao: CustomEndpointDAO = None,
    custom_endpoint_benchmark_dao: CustomEndpointBenchmarkDAO = None,
):
    start_time_provided = start_time is not None
    end_time_provided = end_time is not None
    try:
        user_id = request_fastapi.state.user_id
        available_endpoints = custom_endpoint_dao.filter(
            user_id=user_id,
            name=model,
        )
        for endpoint in available_endpoints:
            if model == endpoint.name:
                endpoint_id = endpoint.id
                break
        else:
            raise HTTPException(
                status_code=400,
                detail=f"""The endpoint: {model} was not found in your account.""",
            )

        short_name_to_db_name = {
            "ttft": "time-to-first-token",
            "itl": "inter-token-latency",
            "input-cost": "input-cost",
            "output-cost": "output-cost",
            "measured-at": "measured-at",
        }
        rets = dict()
        latest_only = not start_time_provided and not end_time_provided
        num_items = 0
        if latest_only:
            start_time = "2024-01-01"
            end_time = str(datetime.now())
        elif not start_time_provided and end_time_provided:
            raise Exception(
                "`start_time` must be provided when" "`end_time` is provided.",
            )
        elif start_time_provided and not end_time_provided:
            end_time = str(datetime.now())
        for short_name, db_name in short_name_to_db_name.items():
            inner_rets = custom_endpoint_benchmark_dao.benchmarks_between(
                endpoint_id=endpoint_id,
                metric_name=db_name,
                start_time=start_time,
                end_time=end_time,
            )
            if inner_rets:
                num_items = len(inner_rets)
                inner_rets.sort(key=lambda x: x.measured_at)
                rets[short_name] = [item.value for item in inner_rets]
            else:
                rets[short_name] = None
        if latest_only:
            single_return = dict()
            for key in rets.keys():
                if rets[key] is None:
                    single_return[key] = None
                else:
                    single_return[key] = rets[key][-1]
            single_return["endpoint"] = model
            return [
                single_return,
            ]
        returns = list()
        for i in range(num_items):
            val = dict()
            for key in rets.keys():
                if rets[key] is None:
                    val[key] = None
                else:
                    val[key] = rets[key][i]
            val["endpoint"] = model
            returns.append(val)
        return returns
    except Exception as e:
        raise e


# endpoints


@router.post(
    "/endpoint-metrics",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {
                        "info": "Custom endpoint benchmark uploaded successfully!",
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
def log_endpoint_metric(
    request_fastapi: Request,
    endpoint_name: str = Query(
        description="Name of the *custom* endpoint to append benchmark data for.",
        example="my_endpoint",
    ),
    metric_name: str = Query(
        description=f"""Name of the metric to submit. Allowed metrics are:
        {ALLOWED_METRICS_STR}.""",
        example="tokens-per-second",
    ),
    value: float = Query(
        description="Value of the metric to submit.",
        example=10,
    ),
    measured_at: datetime = Query(
        default=None,
        description="The timestamp to associate with the submission. "
        "Defaults to current time if unspecified.",
        example="2024-08-12T04:20:32.808410",
    ),
    custom_endpoint_dao: CustomEndpointDAO = Depends(),
    custom_endpoint_benchmark_dao: CustomEndpointBenchmarkDAO = Depends(),
):
    """
    Append speed or cost data to the standardized time-series benchmarks for a custom
    endpoint (only custom endpoints are publishable by end users).
    """
    if metric_name not in ALLOWED_METRICS:
        raise HTTPException(
            status_code=400,
            detail=f"{metric_name} not one of the allowed metrics."
            f"Allowed metrics are: {ALLOWED_METRICS_STR}.",
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
    measured_at = datetime.now() if measured_at is None else measured_at
    custom_endpoint_benchmark_dao.upload_benchmark(
        endpoint_id=endpoint_id,
        metric_name=metric_name,
        value=value,
        measured_at=measured_at,
    )
    return {"info": "Benchmark uploaded!"}


# TODO: Add 404 docstring
@router.get(
    "/endpoint-metrics",
    response_model=List[Dict[str, Union[str, datetime, float, None]]],
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {
                        "ttft": 440.2323939998496,
                        "itl": 8.797065147959705,
                        "input_cost": 0.15,
                        "output_cost": 0.6,
                        "measured_at": "2024-08-17T19:19:37.289937",
                    },
                },
            },
        },
    },
)
def get_endpoint_metrics(
    request_fastapi: Request,
    model: str = Query(
        default=None,
        description="Name of the model.",
        example="gpt-4o-mini",
    ),
    provider: str = Query(
        default=None,
        description="Name of the provider.",
        example="openai",
    ),
    region: str = Query(
        default="Belgium",
        description="""Region where the benchmark is run.
        Options are: `"Belgium"`, `"Hong Kong"` or `"Iowa"`.""",
        example="Belgium",
    ),
    seq_len: str = Query(
        default="short",
        description="Length of the sequence used for benchmarking, "
        "can be short or long",
        example="short",
    ),
    start_time: str = Query(
        default=None,
        description="Window start time. "
        "Only returns the latest benchmark if unspecified",
        example="2024-07-12T04:20:32.808410",
    ),
    end_time: str = Query(
        default=None,
        description="Window end time. Assumed to be the current time if this is "
        "unspecified *and* start_time *is* specified. "
        "Only the latest benchmark is returned if both are unspecified.",
        example="2024-08-12T04:20:32.808410",
    ),
    endpoint_dao: EndpointDAO = Depends(),
    latest_benchmark_dao: LatestBenchmarkDAO = Depends(),
    benchmark_run_dao: BenchmarkRunDAO = Depends(),
    custom_endpoint_dao: CustomEndpointDAO = Depends(),
    custom_endpoint_benchmark_dao: CustomEndpointBenchmarkDAO = Depends(),
):
    """
    Extracts cost and speed data for the provided endpoint via our standardized
    efficiency benchmarks, in the specified region, with the specified sequence length,
    with all benchmark values returned within the specified time window.

    When extracting data for a *custom* endpoint, then `model` is the endpoint name, and
    `provider` must be set as `"custom"`. The arguments `region` and `seq_len` are
    ignored for custom endpoints (they are not publishable).

    If neither `start_time` nor `end_time` are provided, then only the *latest*
    benchmark data is returned. If only `start_time` is provided, then `end_time` is
    assumed to be the current time. An exception is raised if only `end_time` is
    provided.
    """
    start_time_provided = start_time is not None
    end_time_provided = end_time is not None
    latest_only = not start_time_provided and not end_time_provided
    if provider == "custom":
        return _get_custom_endpoint_benchmark(
            request_fastapi,
            model,
            start_time=start_time,
            end_time=end_time,
            custom_endpoint_dao=custom_endpoint_dao,
            custom_endpoint_benchmark_dao=custom_endpoint_benchmark_dao,
        )
    elif os.environ.get("ON_PREM"):
        request_url = os.environ.get("PUBLIC_ORCHESTRA_URL", "") + "/benchmark"
        kwargs = {
            "model": model,
            "provider": provider,
            "region": region,
            "seq_len": seq_len,
            "start_time": start_time,
            "end_time": end_time,
        }
        for key, value in list(kwargs.items()):
            if not value:
                kwargs.pop(key)
        headers = {
            key: value
            for key, value in request_fastapi._headers.items()
            if key in ["content-type", "authorization"]
        }
        response = requests.get(
            request_url,
            params=kwargs,
            headers=headers,
        )
        json_response = response.json()
        if response.status_code != 200:
            raise HTTPException(response.status_code, json_response["detail"])
        return json_response
    try:
        endpoints = _get_endpoint_from_model_provider(model, provider, endpoint_dao)
        if latest_only:
            results = [
                {
                    "benchmark": latest_benchmark_dao.get_latest_benchmarks(
                        endpoint_id=endpoint["id"],
                        regime="concurrent-1",
                        region=region,
                        seq_len=seq_len,
                    ),
                    "endpoint": f'{endpoint["model"]}@{endpoint["provider"]}',
                }
                for endpoint in endpoints
            ]
            results = [
                {
                    "ttft": result["benchmark"][0].ttft,
                    "itl": result["benchmark"][0].itl,
                    "input_cost": result["benchmark"][0].input_cost,
                    "output_cost": result["benchmark"][0].output_cost,
                    "measured_at": result["benchmark"][0].measured_at,
                    "endpoint": result["endpoint"],
                }
                for result in results
                if len(result["benchmark"]) > 0
            ]
            assert len(results) > 0
            return results
        elif not start_time_provided and end_time_provided:
            raise Exception(
                "`start_time` must be provided when" "`end_time` is provided.",
            )
        elif start_time_provided and not end_time_provided:
            end_time = str(datetime.now())
        results = list(
            chain.from_iterable(
                [
                    [
                        {
                            **benchmark,
                            "endpoint": f'{endpoint["model"]}@{endpoint["provider"]}',
                        }
                        for benchmark in benchmark_run_dao.benchmarks_between(
                            endpoint_id=endpoint["id"],
                            start_time=start_time,
                            end_time=end_time,
                            regime="concurrent-1",
                            region=region,
                            seq_len=seq_len,
                        )
                    ]
                    for endpoint in endpoints
                ],
            ),
        )
        assert len(results) > 0
        return results
    except:
        raise not_found(f"Benchmarks for {model}@{provider}")


@router.delete(
    "/endpoint-metrics",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {"info": "Benchmark deleted successfully!"},
                },
            },
        },
    },
)
def delete_endpoint_metrics(
    endpoint_name: str = Query(
        description="Name of the *custom* endpoint to submit a benchmark for.",
        example="my_endpoint",
    ),
):
    """
    Delete *all* benchmark time-series data for a given *custom* endpoint.
    The time-series benchmark data for *public* endpoints are not deletable.
    """
    raise NotImplemented  # ToDo: implement
