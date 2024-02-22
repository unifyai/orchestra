import logging
import time
from typing import Callable

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

# HTTP responses

insufficient_credits_error = HTTPException(
    status_code=402,
    detail=(
        "Whoops! It seems like this account doesn't have enough credits. "
        "To get a recharge, visit https://console.unify.ai/"
    ),
)

# Performance based dynamic routing

_performance_lut = {}


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


def _compute_lowest(metrics_dict):
    lowest_metrics = {}  # Dictionary to store the lowest metrics by provider
    # Iterate over each provider and their metrics
    for provider, metrics in metrics_dict.items():
        # Iterate over each metric for the current provider
        for metric, value in metrics.items():
            # If the metric is not yet in the lowest_metrics dictionary or its value is lower than the stored value
            if metric not in lowest_metrics or value < lowest_metrics[metric]["value"]:
                # Update the lowest_metrics dictionary with the new lowest value
                lowest_metrics[metric] = {"provider": provider, "value": value}
    return lowest_metrics


def update_performance_lut(model, model_dao, benchmark_run_dao, datapoint_dao):
    logging.info("Updating performance LUT.")
    if model in _performance_lut and _lt_hours(_performance_lut[model]["ts"]):
        return
    model_id = model_dao.filter(mdl_code=model)[0].id
    _performance_lut[model] = {
        "ts": time.time(),
        "metrics": _get_metrics(model_id, benchmark_run_dao, datapoint_dao),
    }
    _performance_lut[model]["lowest"] = _compute_lowest(
        _performance_lut[model]["metrics"],
    )

performance_rules = [
    "lowest-input-cost",
    "lowest-outut-cost",
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

def performance_based_routing(
    model,
    provider,
    model_dao,
    benchmark_run_dao,
    datapoint_dao,
):
    if provider not in performance_rules:
        raise HTTPException(
            status_code=400,  # noqa: WPS432
            detail=f"Invalid input. Provider has to be one of {performance_rules} when doing performance routing.",
        )
    update_performance_lut(model, model_dao, benchmark_run_dao, datapoint_dao)
    provider = _aliases(provider)
    criterium, metric = provider.split("-", 1)
    metric = metric.replace("-", "_")
    if metric == "highest_output_tks_per_sec":
        metric = "lowest_itl"
    return _performance_lut[model][criterium][metric]["provider"]


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
        "function_call",
        "functions",
        "stream",
    ]
    return {
        param: arguments.get(param)
        for param in openai_params
        if arguments.get(param) is not None
    }
