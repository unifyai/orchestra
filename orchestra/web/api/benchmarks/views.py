"""
Includes endpoints related to benchmarks.
"""

import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.param_functions import Depends

from orchestra.db.dao.benchmark_run_dao import BenchmarkRunDAO
from orchestra.db.dao.custom_endpoint_benchmark_dao import CustomEndpointBenchmarkDAO
from orchestra.db.dao.custom_endpoint_dao import CustomEndpointDAO
from orchestra.db.dao.endpoint_dao import EndpointDAO
from orchestra.db.dao.latest_benchmark_dao import LatestBenchmarkDAO
from orchestra.db.dao.query_dao import QueryDAO
from orchestra.web.api.utils.http_responses import benchmark_not_found, model_not_found
from orchestra.web.api.utils.on_prem import handle_on_prem

router = APIRouter()


def _get_endpoint_from_model_provider(
    model: str,
    provider: str,
    endpoint_dao: EndpointDAO,
):
    try:
        endpoint_id = endpoint_dao.get_endpoints_of(
            models=(model,),
            only_from=(provider,),
        )
        endpoint_id = endpoint_id[0][0].id
        return endpoint_id
    except:
        raise model_not_found


@router.get("/benchmarks")
def get_latest_benchmark(
    request_fastapi: Request,
    model: str = Query(..., description="Model name", example="gpt-4o-mini"),
    provider: str = Query(..., description="Provider name", example="openai"),
    regime: str = Query(default="concurrent-1", example="concurrent-1"),
    region: str = Query(
        default="Belgium",
        description="""Region where the benchmark is run. Options are: "Belgium", "Hong Kong", "Iowa".""",
        example="Belgium",
    ),
    seq_len: str = Query(
        default="short",
        description="Length of the sequence used for benchmarking, can be short or long",
        example="short",
    ),
    endpoint_dao: EndpointDAO = Depends(),
    latest_benchmark_dao: LatestBenchmarkDAO = Depends(),
    custom_endpoint_dao: CustomEndpointDAO = Depends(),
    custom_endpoint_benchmark_dao: CustomEndpointBenchmarkDAO = Depends(),
):
    if provider == "custom":
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
            }
            ret = {}
            for short_name, db_name in short_name_to_db_name.items():
                results = custom_endpoint_benchmark_dao.benchmarks_between(
                    endpoint_id=endpoint_id,
                    metric_name=db_name,
                    start_time="2024-01-01",
                    end_time=str(datetime.datetime.now()),
                )
                if results:
                    results.sort(key=lambda x: x.measured_at)
                    result = results[-1].value
                else:
                    result = None
                ret[short_name] = result
            ret["measured_at"] = None
            return ret
        except:
            raise Exception
    try:
        endpoint_id = _get_endpoint_from_model_provider(model, provider, endpoint_dao)
        result = latest_benchmark_dao.get_latest_benchmarks(
            endpoint_id=endpoint_id,
            regime=regime,
            region=region,
            seq_len=seq_len,
        )
        result = result[0]
        ret = {
            "ttft": result.ttft,
            "itl": result.itl,
            "input_cost": result.input_cost,
            "output_cost": result.output_cost,
            "measured_at": result.measured_at,
        }
        return ret
    except:
        raise benchmark_not_found(f"{model}@{provider}")


@router.post("/benchmarks/filter")
@handle_on_prem(endpoint="/benchmarks/filter", method="post")
def filter_benchmark(
    model: str = Query(..., description="Model name", example="gpt-4o-mini"),
    provider: str = Query(..., description="Provider name", example="openai"),
    start_time: str = Query(
        ...,
        description="Window start time",
        example="2024-07-12T04:20:32.808410",
    ),
    end_time: str = Query(
        ...,
        description="Window end time",
        example="2024-08-12T04:20:32.808410",
    ),
    regime: str = Query(default="concurrent-1", example="concurrent-1"),
    region: str = Query(
        default="Belgium",
        description="""Region where the benchmark is run. Options are: "Belgium", "Hong Kong", "Iowa".""",
        example="Belgium",
    ),
    seq_len: str = Query(
        default="short",
        description="""Length of the sequence used for benchmarking. Options are: "short", "long".""",
        example="short",
    ),
    endpoint_dao: EndpointDAO = Depends(),
    benchmark_run_dao: BenchmarkRunDAO = Depends(),
):
    try:
        endpoint_id = _get_endpoint_from_model_provider(model, provider, endpoint_dao)
        result = benchmark_run_dao.benchmarks_between(
            endpoint_id=endpoint_id,
            start_time=start_time,
            end_time=end_time,
            regime=regime,
            region=region,
            seq_len=seq_len,
        )
        return result
    except:
        raise benchmark_not_found(f"{model}@{provider}")
