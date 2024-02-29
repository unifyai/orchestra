import hashlib
import logging
import time
from typing import Any, Callable, Dict

from fastapi import HTTPException

from orchestra.db.dao.endpoint_dao import EndpointDAO
from orchestra.db.dao.model_dao import ModelDAO
from orchestra.db.dao.provider_dao import ProviderDAO
from orchestra.db.dao.query_dao import QueryDAO
from orchestra.db.dao.users_dao import UsersDAO
from orchestra.web.api.endpoint.views import get_endpoint
from orchestra.web.api.model.views import get_model
from orchestra.web.api.provider.views import get_provider
from orchestra.web.api.query.schema import QueryModelRequest
from orchestra.web.api.query.views import create_query_model

logger = logging.getLogger(__name__)

# HTTP responses

insufficient_credits_error = HTTPException(
    status_code=402,
    detail=(
        "Whoops! It seems like this account doesn't have enough credits. "
        "To get a recharge, visit https://console.unify.ai/"
    ),
)


# TODO: Test this
def server_error_with_digest(text: str):
    digest = hashlib.shake_256(text.encode()).digest(4).hex()
    return (
        HTTPException(
            status_code=500,
            detail=f"Internal Server Error. Digest: {digest}",
        ),
        digest,
    )


# Performance based dynamic routing

"""
This LUT acts as a cache for performance based routing. The structure is:
{
    "<model-id>": {
        "metrics": {
            "ts": timestamp of the last update of the metrics, time.time()
            "<provider>": {
                "<metric-name>": metric.value
            }
        }
        "lowest": {
            "<metric-name>": {
                "[float]ic": {
                    "ts": timestamp of the last update
                    "provider": "<provider>"
                    "value": "<value>"
                }
                "[float]oc": {
                    ...
                }
            }
        }
    }
}
# TODO: Move this into a class
"""
_performance_lut: Dict[str, Any] = {}


def _lt_hours(ts, n=3):
    return (time.time() - ts) < (n * 3600)


def _get_metrics(model_id, benchmark_run_dao, datapoint_dao):
    brs = benchmark_run_dao.get_model_benchmark_runs(model_id)
    providers = {}
    # Generate lists of the metrics for each provider
    for br in brs:
        metrics = datapoint_dao.filter(benchmark_run_id=br.BenchmarkRun.id)
        providers[br.Provider.name] = {}
        for metric in metrics:
            providers[br.Provider.name][metric.metric_name] = metric.value
    return providers


def update_performance_lut(model, model_dao, benchmark_run_dao, datapoint_dao):
    logger.info("Updating performance LUT.")
    if model in _performance_lut and _lt_hours(_performance_lut[model]["ts"]):
        return
    try:
        model_id = model_dao.filter(mdl_code=model)[0].id
    except IndexError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid input. model-id doesn't match any entry in the model hub.",
        )
    _performance_lut[model] = {
        "ts": time.time(),
        "metrics": _get_metrics(model_id, benchmark_run_dao, datapoint_dao),
    }


performance_rules = [
    "lowest-input-cost",
    "lowest-output-cost",
    "lowest-input-cost-per-token",
    "lowest-output-cost-per-token",
    "lowest-itl",
    "highest-tks-per-sec",
    "highest-output-tks-per-sec",
    "lowest-ttft",
]


def _aliases(metric):
    return {
        "lowest-input-cost": "lowest-input-cost-per-token",
        "lowest-output-cost": "lowest-output-cost-per-token",
        "highest-tks-per-sec": "lowest-itl",
        "highest-output-tks-per-sec": "lowest-itl",
    }.get(metric, metric)


invalid_price_breakpoint = HTTPException(
    status_code=400,
    detail=(
        "Invalid price breakpoint. Format needs to be config<[float][ic|oc]. "
        "See https://unify.ai/docs/hub/concepts/runtime_routing.html#price-breakpoints for more details."
    ),
)


def valid_price_breakpoint(price_breakpoint):
    if price_breakpoint == "inf<>":
        return True
    if price_breakpoint[-2:] not in ["ic", "oc"]:
        return False
    try:
        float(price_breakpoint[:-2])
    except ValueError:
        return False
    return True


def _compute_lowest(model, criterium, metric, price_breakpoint):
    # TODO: Deal with expired measurements
    metrics_dict = _performance_lut[model]["metrics"]
    # TODO: Add support for this
    comparation_fn = {
        "highest": max,
        "lowest": min,
    }[criterium]
    brkp_value = float(price_breakpoint[:-2])
    brkp_metric = {
        "<>": None,
        "ic": "input_cost_per_token",
        "oc": "output_cost_per_token",
    }[price_breakpoint[-2:]]

    optimal_metric = {}
    # Iterate over each provider
    for provider, provider_metrics in metrics_dict.items():
        value = provider_metrics[metric]
        if brkp_metric and provider_metrics[brkp_metric] >= brkp_value:
            continue
        if not optimal_metric or value < optimal_metric["value"]:
            optimal_metric = {"provider": provider, "value": value}
    if not optimal_metric:
        return -1
    return optimal_metric["provider"]


def _get_provider_from_lut(model, criterium, metric, price_breakpoint):
    if criterium not in _performance_lut[model]:
        _performance_lut[model][criterium] = {}
    if metric not in _performance_lut[model][criterium]:
        _performance_lut[model][criterium][metric] = {}
    if price_breakpoint not in _performance_lut[model][criterium][metric]:
        _performance_lut[model][criterium][metric][price_breakpoint] = _compute_lowest(
            model,
            criterium,
            metric,
            price_breakpoint,
        )
    return _performance_lut[model][criterium][metric][price_breakpoint]


def performance_based_routing(
    model,
    provider: str,
    model_dao,
    benchmark_run_dao,
    datapoint_dao,
):
    price_breakpoint = "inf<>"
    if ">" in provider:
        raise invalid_price_breakpoint
    if "<" in provider:
        provider, price_breakpoint = provider.split("<", 1)
    if not valid_price_breakpoint(price_breakpoint):
        raise invalid_price_breakpoint
    if provider not in performance_rules:
        # TODO: Move this to exception file
        raise HTTPException(
            status_code=400,
            detail=f"Invalid input. Provider has to be one of {performance_rules} when doing performance routing.",
        )

    # refresh metrics if needed
    update_performance_lut(model, model_dao, benchmark_run_dao, datapoint_dao)

    provider = _aliases(provider)
    criterium, metric = provider.split("-", 1)
    metric = metric.replace("-", "_")
    try:
        ret = _get_provider_from_lut(model, criterium, metric, price_breakpoint)
        if ret == -1:
            raise HTTPException(
                status_code=404,
                detail="No providers found within the specified price limits.",
            )
    except KeyError as e:  # TODO: Prob move this inside the other function
        server_error, digest = server_error_with_digest(str(e))
        logger.error(f"Digest {digest}: {str(e)}")
        raise server_error
    return ret


insufficient_credits_error = HTTPException(
    status_code=402,
    detail=(
        "Whoops! It seems like this account doesn't have enough credits. "
        "To get a recharge, visit https://console.unify.ai/"
    ),
)

# Background tasks


def db_operations(  # noqa: WPS211, WPS217, WPS210
    user_id: str,
    cost_deferred_fn: Callable,
    model: str,
    provider: str,
    model_dao: ModelDAO,
    provider_dao: ProviderDAO,
    endpoint_dao: EndpointDAO,
    query_dao: QueryDAO,
    users_dao: UsersDAO,
):
    """
    Perform database operations.

    :param user_id: user id.
    :param cost_deferred_fn: deferred cost computation of the operation.
    :param model: model name.
    :param provider: provider name.
    :param model_dao: DAO for model models.
    :param provider_dao: DAO for provider models.
    :param endpoint_dao: DAO for endpoint models.
    :param query_dao: DAO for query models.
    :param users_dao: DAO for users models.

    :raises HTTPException: when endpoint is not found.
    """
    model_id = int(get_model(mdl_code=model, model_dao=model_dao)[0].id)
    provider_id = int(get_provider(name=provider, provider_dao=provider_dao)[0].id)
    endpoint_ids = get_endpoint(
        mdl_id=model_id,
        provider_id=provider_id,
        endpoint_dao=endpoint_dao,
        model_dao=model_dao,
        provider_dao=provider_dao,
    )
    endpoint_id = next(
        (
            int(endpoint.endpoint_id)
            for endpoint in endpoint_ids
            if endpoint.provider_id == provider_id
        ),
        None,
    )
    if endpoint_id is None:
        raise HTTPException(
            status_code=500,  # noqa: WPS432
            detail="Endpoint not found",
        )
    cost = cost_deferred_fn()
    query_model_request = QueryModelRequest(
        user_id=user_id,
        endpoint_id=endpoint_id,
        credits=cost,  # type: ignore
    )
    users_dao.recharge_credit(user_id, -cost)
    create_query_model(query_model_request, query_dao=query_dao)


def filter_request_params(arguments):
    """
    Filter argument parameters.

    :param arguments: arguments object.

    :return: dictionary of filtered parameters.
    """
    openai_params = [
        "frequency_penalty",
        "logit_bias",
        "logprobs",
        "top_logprobs",
        "max_tokens",
        "n",
        "presence_penalty",
        "response_format",
        "seed",
        "stop",
        "stream",
        "temperature",
        "top_p",
        "tools",
        "tool_choice",
        "user",
        "stream",
    ]
    return {
        param: arguments.get(param)
        for param in openai_params
        if arguments.get(param) is not None
    }
