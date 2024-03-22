from typing import Dict
import logging

from fastapi import APIRouter
from fastapi.param_functions import Depends

from orchestra.db.dao.model_dao import ModelDAO
from orchestra.db.dao.benchmark_run_dao import BenchmarkRunDAO
from orchestra.web.api.benchmarks.schema import BenchmarksRequest, BenchmarksResponse
from orchestra.web.api.utils.http_responses import server_error_with_digest

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

handler = logging.StreamHandler()
handler.setLevel(logging.INFO)
logger.addHandler(handler)

router = APIRouter()


@router.get("/benchmarks/data", response_model=BenchmarksResponse)
def get_benchmark_data(
    request: BenchmarksRequest,
    model_dao: ModelDAO = Depends(),
    benchmark_run_dao: BenchmarkRunDAO = Depends(),
) -> BenchmarksResponse:
    """
    Return the latest metrics for a given model@provider in a region and seq len.
    """
    # TODO: Check that fields have been specified
    # TODO: Substract credits
    # TODO: Add docs
    model = model_dao.filter(mdl_code=request.model)
    try:
        model_id = model[0].id
        brs = benchmark_run_dao.get_model_benchmark_datapoints(
            model_id,
            region=request.region,
            seq_len=request.seq_len,
        )
        if not brs:
            raise ValueError
        metrics: Dict[str, Dict[str, float]] = {}
        for br in brs:
            if br.Provider.name == request.provider:
                metrics[br.Datapoint.metric_name] = float(br.Datapoint.value)
    except (IndexError, AttributeError, ValueError):
        error_str = (
            f"No Benchmark data found for {request.model_code}@{request.provider} in",
            f" region ({request.region}) and SeqLen ({request.seq_len})",
        )
        error, digest = server_error_with_digest(error_str)
        logger.error(f"Digest {digest}: {error_str}")
        raise error
    return BenchmarksResponse(
        model=request.model, provider=request.provider, metrics=metrics
    )
