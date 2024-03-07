# TODO: lru cache needs to be changed to a bg task
import logging
import re
import time
from collections import namedtuple
from functools import lru_cache
from typing import Dict, List, Optional, Tuple

from orchestra.db.dao.benchmark_run_dao import BenchmarkRunDAO
from orchestra.db.dao.endpoint_dao import EndpointDAO

# TODO: Add errors back to the refactored function
from orchestra.web.api.utils.http_responses import (  # invalid_optimisation_goal,; invalid_price_threshold,
    provider_not_found_under_conditions,
    server_error_with_digest,
)

logger = logging.getLogger(__name__)


def metric_aliases(metric):
    return {
        "ic": "input_cost_per_token",
        "input-cost": "input_cost_per_token",
        "lowest-input-cost": "input_cost_per_token",
        "lowest-input-cost-per-token": "input_cost_per_token",
        "oc": "output_cost_per_token",
        "output-cost": "output_cost_per_token",
        "lowest-output-cost": "output_cost_per_token",
        "lowest-output-cost-per-token": "output_cost_per_token",
        "ots": "itl",
        "tks-per-sec": "itl",
        "highest-tks-per-sec": "itl",
        "output-tks-per-sec": "itl",
        "highest-output-tks-per-sec": "itl",
        "lowest-itl": "itl",
        "lowest-ttft": "ttft",
    }.get(metric, metric)


Endpoint = namedtuple(
    "Endpoint",
    ["id", "model", "model_id", "provider", "provider_id"],
)


def get_ttl_hash(seconds=3600):
    """Return the same value within `seconds` time period"""
    return round(time.time() / seconds)


@lru_cache()
def get_endpoints_of(
    endpoint_dao: EndpointDAO,
    models: List[str],
    ttl_hash: int,
    only_from: Optional[List[str]] = None,
) -> List[Endpoint]:
    del ttl_hash
    # TODO: Ensure that the DAO is not messing up the lru cache
    logger.info(f"Getting endpoints of {models}")
    query_result = endpoint_dao.get_endpoints_of(models, only_from)
    if not query_result:
        error_str = f"No Endpoints found for {models} (only_from: {only_from})"
        error, digest = server_error_with_digest(error_str)
        logger.error(f"Digest {digest}: {error_str}")
        raise error
    return [
        Endpoint(
            q.Endpoint.id,
            q.Model.mdl_code,
            q.Model.id,
            q.Provider.name,
            q.Provider.id,
        )
        for q in query_result
    ]


@lru_cache()
def get_model_metrics(
    benchmark_run_dao: BenchmarkRunDAO,
    endpoint: Endpoint,
    ttl_hash: int,
):
    del ttl_hash
    logger.info(f"Getting metrics for {endpoint}")
    brs = benchmark_run_dao.get_model_benchmark_datapoints(endpoint.model_id)
    if not brs:
        # TODO: test this
        error_str = f"No BenchmarkRuns found for {endpoint}"
        error, digest = server_error_with_digest(error_str)
        logger.error(f"Digest {digest}: {error_str}")
        raise error
    metrics: Dict[str, Dict[str, float]] = {}
    for br in brs:
        if br.Provider.name not in metrics:
            metrics[br.Provider.name] = {}
        metrics[br.Provider.name][br.Datapoint.metric_name] = br.Datapoint.value
    return metrics


def get_value_of(
    benchmark_run_dao: BenchmarkRunDAO,
    endpoint: Endpoint,
    metric: str,
) -> float:
    model_metrics = get_model_metrics(
        benchmark_run_dao,
        endpoint,
        ttl_hash=get_ttl_hash(),
    )
    return model_metrics[endpoint.provider][metric]


def find_best(
    benchmark_run_dao: BenchmarkRunDAO,
    endpoints: List[Endpoint],
    metric: str,
) -> Tuple[str, str]:
    def _get_metric_value(endpoint: Endpoint) -> float:
        model_metrics = get_model_metrics(
            benchmark_run_dao,
            endpoint,
            ttl_hash=get_ttl_hash(),
        )
        return model_metrics[endpoint.provider][metric]

    selected_endpoint = min(endpoints, key=_get_metric_value)
    return selected_endpoint.model, selected_endpoint.provider


def threshold_endpoints(
    benchmark_run_dao: BenchmarkRunDAO,
    endpoints: List[Endpoint],
    metrics_thresholds: Dict[str, float],
) -> List[Endpoint]:
    valid_endpoints = []
    for endpoint in endpoints:
        is_valid = True
        for metric, threshold in metrics_thresholds.items():
            if get_value_of(benchmark_run_dao, endpoint, metric) >= threshold:
                is_valid = False
        if is_valid:
            valid_endpoints.append(endpoint)
    return valid_endpoints


def convert_threshold(metric, value):
    fn = {
        "ots": lambda x: 1 / x,
        "tks-per-sec": lambda x: 1 / x,
        "highest-tks-per-sec": lambda x: 1 / x,
        "output-tks-per-sec": lambda x: 1 / x,
        "highest-output-tks-per-sec": lambda x: 1 / x,
    }.get(metric, lambda x: x)
    return fn(value)


def standarise_thresholds(threhsolds):
    clean_thresholds = {}
    for old_metric, old_value in threhsolds.items():
        new_metric = metric_aliases(old_metric)
        new_value = convert_threshold(old_metric, old_value)
        clean_thresholds[new_metric] = new_value
    return clean_thresholds


def parse_endpoint(endpoint: str):

    # TODO: Raise error if not correctly formated or not valid metric

    main_metric = endpoint.split("<", 1)[0].split(">")[0]

    # Regular expression pattern to match the thresholds
    pattern = r"(?P<operator>[<>])(?P<value>\d+\.*\d*)(?P<unit>\w*)"

    # Search for matches using the pattern
    matches = re.findall(pattern, endpoint.removeprefix(main_metric))

    # Initialize variables to store thresholds
    thresholds = {}

    for match in matches:
        value = float(match[1])
        metric = match[2]

        # Store the threshold in the dictionary
        thresholds[metric] = value

    main_metric = metric_aliases(main_metric)
    thresholds = standarise_thresholds(thresholds)

    return main_metric, thresholds


def dynamic_routing(
    endpoint_dao: EndpointDAO,
    benchmark_run_dao: BenchmarkRunDAO,
    target_metric: str,
    user_config: Optional[str] = None,
    models: Optional[Tuple[str, ...]] = None,
    providers: Optional[Tuple[str, ...]] = None,
    router_threshold: float = 0,
    metrics_thresholds: Optional[Dict[str, float]] = None,
) -> Tuple[str, str]:
    # If user_config is specified, override params.
    # The configs should be cached, if there is no cache, the config
    # is queried from the DB. If there is cache, we get it from there
    # and then start a background task to update the config in the cache.
    if user_config:
        raise NotImplementedError("Users configs are not available yet.")
    if not models or len(models) != 1:
        raise NotImplementedError(
            "Performance based routing is not available yet. Only one model can be specified.",
        )
    # Get all endpoints from the specified models x providers.
    # If providers is not None, only endpoints from these providers will be considered.
    # TODO: Implement function to get these from cache / DB + background task?
    endpoints = get_endpoints_of(
        endpoint_dao,
        models,
        only_from=providers,
        ttl_hash=get_ttl_hash(),
    )
    # Remove endpoints that don't fall within the metrics thresholds
    if metrics_thresholds:
        thresholded_endpoints = threshold_endpoints(
            benchmark_run_dao,
            endpoints,
            metrics_thresholds,
        )
    if not thresholded_endpoints:
        raise provider_not_found_under_conditions
    # Extract models from thresholded_endpoints
    thresholded_models = set([endpoint.model for endpoint in thresholded_endpoints])
    # Pass this to the router to get scores for each model
    # TODO: Implement this properly, it should return a Dict[str, float] of
    # model_id:score
    # router_scores = score_models(models, messages)
    # Get the first one since there should only be one
    router_scores = {list(thresholded_models)[0]: 1}
    non_valid_models = set(
        [model for model, score in router_scores.items() if score < router_threshold],
    )
    # Remove non_valid_models from thresholded_endpoints
    valid_endpoints = []
    for endpoint in thresholded_endpoints:
        if endpoint.model not in non_valid_models:
            valid_endpoints.append(endpoint)
    # TODO: Raise error if no valid endpoints
    # Now we have a list of valid endpoints
    # Get the (model, provider) combination that optimises the target metric
    selected_model, selected_provider = find_best(
        benchmark_run_dao,
        valid_endpoints,
        target_metric,
    )
    return selected_model, selected_provider


# TODO: Change previous dynamic routing to use the new function
# TODO: Parse multiple thresholds
# TODO: Deal with aliases of metrics
# TODO: Connect new function with chat completions
# TODO: Connect new funciton with inference
# TODO: Check lru cache with daos
