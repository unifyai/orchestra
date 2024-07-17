"""
Includes endpoints related to benchmarks.
"""

from fastapi import APIRouter
from fastapi.param_functions import Depends

from orchestra.db.dao.endpoint_dao import EndpointDAO
from orchestra.db.dao.latest_benchmark_dao import LatestBenchmarkDAO

router = APIRouter()


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
        endpoint_id = endpoint_dao.get_endpoints_of(
            models=(model,), only_from=(provider,)
        )
        endpoint_id = endpoint_id[0][0].id
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
        raise ValueError(
            f"We couldn't find benchmarks for {model}@{provider}, please check again."
        )
