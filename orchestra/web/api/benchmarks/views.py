"""
Includes endpoints related to benchmarks.
"""

from fastapi import APIRouter
from fastapi.param_functions import Depends

from orchestra.db.dao.endpoint_dao import EndpointDAO
from orchestra.db.dao.latest_benchmark_dao import LatestBenchmarkDAO
from orchestra.db.dao.benchmark_run_dao import BenchmarkRunDAO
from orchestra.web.api.utils.http_responses import benchmark_not_found, model_not_found

router = APIRouter()


def _get_endpoint_from_model_provider(
    model: str, provider: str, endpoint_dao: EndpointDAO
):
    try:
        endpoint_id = endpoint_dao.get_endpoints_of(
            models=(model,), only_from=(provider,)
        )
        endpoint_id = endpoint_id[0][0].id
        return endpoint_id
    except:
        raise model_not_found


@router.get("/benchmarks")
def get_latest_benchmark(
    model: str,
    provider: str,
    regime: str = "concurrent-1",
    region: str = "Belgium",
    seq_len: str = "short",
    endpoint_dao: EndpointDAO = Depends(),
    latest_benchmark_dao: LatestBenchmarkDAO = Depends(),
):
    try:
        endpoint_id = _get_endpoint_from_model_provider(model, provider, endpoint_dao)
        result = latest_benchmark_dao.get_latest_benchmarks(
            endpoint_id=endpoint_id, regime=regime, region=region, seq_len=seq_len
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
def filter_benchmark(
    model: str,
    provider: str,
    start_time: str,
    end_time: str,
    regime: str = "concurrent-1",
    region: str = "Belgium",
    seq_len: str = "short",
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
