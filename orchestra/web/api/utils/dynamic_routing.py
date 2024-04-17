# TODO: lru cache needs to be changed to a bg task
import logging
import math
import re
import time
from collections import namedtuple
from typing import Dict, List, Optional, Tuple, Union

from google.cloud import aiplatform

from orchestra.db.dao.benchmark_run_dao import BenchmarkRunDAO
from orchestra.db.dao.endpoint_dao import EndpointDAO
from orchestra.settings import settings

# TODO: Add errors back to the refactored function
from orchestra.web.api.utils.http_responses import (  # invalid_optimisation_goal,; invalid_price_threshold,
    invalid_provider_str,
    provider_not_found_under_conditions,
    server_error_with_digest,
)

logger = logging.getLogger(__name__)

### Arbitrary function


class RouterConfig:
    def __init__(
        self,
        endpoint_str: str,
        endpoint_dao: EndpointDAO,
        benchmark_run_dao: BenchmarkRunDAO,
    ):
        assert "router" in endpoint_str

        self.endpoint_str = endpoint_str
        self.endpoint_dao = endpoint_dao
        self.benchmark_run_dao = benchmark_run_dao

        self.info_segments = self.endpoint_str_to_dict()

        self.default_models = {
            "claude-3-haiku",
            "claude-3-sonnet",
            "deepseek-coder-33b-instruct",
            "gemma-7b-it",
            "gpt-3.5-turbo",
            "gpt-4",
            "mistral-large",
            "mistral-small",
            "mixtral-8x7b-instruct-v0.1",
        }
        self.models = self.extract_list("models")

        self.default_providers = {
            "anthropic",
            "together-ai",
            "mistral-ai",
            "openai",
            "anyscale",
            "fireworks-ai",
            "deepinfra",
            "octoai",
            "aws-bedrock",
        }
        self.providers = self.extract_list("providers")

        self.q = self.extract_factor("q")
        self.c = self.extract_factor("c")
        self.i = self.extract_factor("i")
        self.t = self.extract_factor("t")

        self.thresholds = {}
        self.thresholds["quality"] = self.extract_thrs("quality")
        self.thresholds["cost"] = self.extract_thrs("cost")
        self.thresholds["itl"] = self.extract_thrs("itl")
        self.thresholds["ttft"] = self.extract_thrs("ttft")

    def endpoint_str_to_dict(self):
        provider_substr = self.endpoint_str.split("@")[1]
        pairs = provider_substr.split("|")
        dict_out = {}
        for p in pairs:
            items = p.split(":")
            dict_out[items[0]] = items[1]
        return dict_out

    def extract_list(self, attr):
        out = getattr(self, f"default_{attr}")
        if attr in self.info_segments:
            specified = set(self.info_segments[attr].split(","))
            out = out.intersection(specified)
        return out

    def extract_factor(self, attr, default=0):
        out = default
        if attr in self.info_segments:
            out = float(self.info_segments[attr])
        return out

    def extract_thrs(self, attr):
        full_attr = f"{attr}_thrs"
        if full_attr in self.info_segments:
            items = self.info_segments[full_attr].split(",")
            return (float(items[0]), float(items[1]))
        return (None, None)

    def cost_fn(self, quality, cost, itl, ttft, **kwargs):
        return -self.q * quality + self.c * cost + self.i * itl + self.t * ttft

    def __call__(self, prompt):
        # Get full list of endpoints
        endpoints = get_endpoints_of(
            self.endpoint_dao,
            tuple(self.models),
            only_from=tuple(self.providers),
            ttl_hash=get_ttl_hash(),
        )
        # Get quality from the neural router scoring function
        model_scores = neural_scoring(prompt)

        endpoint_metrics = {}
        thresholded_endpoints = []
        for endpoint in endpoints:
            name = f"{endpoint.model}@{endpoint.provider}"
            endpoint_metrics[name] = {}
            endpoint_metrics[name]["quality"] = model_scores[endpoint.model]
            for metric in [
                "input_cost_per_token",
                "output_cost_per_token",
                "ttft",
                "itl",
            ]:
                endpoint_metrics[name][metric] = float(
                    get_value_of(self.benchmark_run_dao, endpoint, metric),
                )
            endpoint_metrics[name]["cost"] = (
                endpoint_metrics[name]["input_cost_per_token"] * 3
                + endpoint_metrics[name]["output_cost_per_token"]
            ) / 4

            valid = True
            for metric, threshold in self.thresholds.items():
                if (
                    threshold[0] is not None
                    and endpoint_metrics[name][metric] < threshold[0]
                ):
                    valid = False
                if (
                    threshold[1] is not None
                    and endpoint_metrics[name][metric] > threshold[1]
                ):
                    valid = False
            if valid:
                thresholded_endpoints.append(endpoint)

        if not thresholded_endpoints:
            raise provider_not_found_under_conditions

        endpoint_scores = {}
        for endpoint in thresholded_endpoints:
            name = f"{endpoint.model}@{endpoint.provider}"
            endpoint_scores[name] = self.cost_fn(**endpoint_metrics[name])

        return min(endpoint_scores, key=lambda k: endpoint_scores[k]).split("@")


def neural_scoring(prompt):
    # TODO: Initialise the VertexAI acc outside

    endpoint = aiplatform.Endpoint(settings.vertexai_router_endpoint_id)
    prediction = endpoint.predict(instances=[{"prompt": prompt}])
    out = prediction.predictions[0]["scores"]
    out["gpt-4"] = out.pop("gpt-4-0125-preview")
    return out


def metric_aliases(metric):
    return {
        "ic": "input_cost_per_token",
        "input-cost": "input_cost_per_token",
        "input-cost-per-token": "input_cost_per_token",
        "lowest-input-cost": "input_cost_per_token",
        "lowest-input-cost-per-token": "input_cost_per_token",
        "oc": "output_cost_per_token",
        "output-cost": "output_cost_per_token",
        "output-cost-per-token": "output_cost_per_token",
        "lowest-output-cost": "output_cost_per_token",
        "lowest-output-cost-per-token": "output_cost_per_token",
        "ots": "itl",
        "tks-per-sec": "itl",
        "highest-tks-per-sec": "itl",
        "output-tks-per-sec": "itl",
        "highest-output-tks-per-sec": "itl",
        "lowest-itl": "itl",
        "itl": "itl",
        "lowest-ttft": "ttft",
        "ttft": "ttft",
    }.get(metric, None)


Endpoint = namedtuple(
    "Endpoint",
    ["id", "model", "model_id", "provider", "provider_id"],
)


def get_ttl_hash(seconds=3600):
    """Return the same value within `seconds` time period"""
    return round(time.time() / seconds)


_cached_endpoints: Dict[str, Dict[str, Union[int, List[Endpoint]]]] = {}


def get_endpoints_of(
    endpoint_dao: EndpointDAO,
    models: Tuple[str, ...],
    ttl_hash: int,
    only_from: Optional[Tuple[str, ...]] = None,
) -> List[Endpoint]:
    full_hash = str(hash(models)) + str(hash(only_from))

    if (
        full_hash in _cached_endpoints
        and _cached_endpoints[full_hash].get("ttl_hash", 0) == ttl_hash
    ):
        return _cached_endpoints[full_hash]["endpoints"]  # type: ignore[return-value]
    logger.info(f"Getting endpoints of {models}")
    query_result = endpoint_dao.get_endpoints_of(models, only_from)
    if not query_result:
        error_str = f"No Endpoints found for {models} (only_from: {only_from})"
        error, digest = server_error_with_digest(error_str)
        logger.error(f"Digest {digest}: {error_str}")
        raise error
    endpoints = [
        Endpoint(
            q.Endpoint.id,
            q.Model.mdl_code,
            q.Model.id,
            q.Provider.name,
            q.Provider.id,
        )
        for q in query_result
    ]
    _cached_endpoints[full_hash] = {}
    _cached_endpoints[full_hash]["endpoints"] = endpoints
    _cached_endpoints[full_hash]["ttl_hash"] = ttl_hash
    return endpoints


_cached_metrics: Dict[Endpoint, Dict[str, Union[int, Dict[str, Dict[str, float]]]]] = {}


def get_model_metrics(
    benchmark_run_dao: BenchmarkRunDAO,
    endpoint: Endpoint,
    ttl_hash: int,
):
    if (
        endpoint in _cached_metrics
        and _cached_metrics[endpoint].get("ttl_hash", 0) == ttl_hash
    ):
        return _cached_metrics[endpoint]["metrics"]
    logger.info(f"Getting metrics for {endpoint}")
    brs = benchmark_run_dao.get_model_benchmark_datapoints(endpoint.model_id)
    if not brs:
        # TODO: add test for this
        error_str = f"No BenchmarkRuns found for {endpoint}"
        error, digest = server_error_with_digest(error_str)
        logger.error(f"Digest {digest}: {error_str}")
        raise error
    metrics: Dict[str, Dict[str, float]] = {}
    for br in brs:
        if br.Provider.name not in metrics:
            metrics[br.Provider.name] = {}
        metrics[br.Provider.name][br.Datapoint.metric_name] = br.Datapoint.value
    _cached_metrics[endpoint] = {}
    _cached_metrics[endpoint]["metrics"] = metrics
    _cached_metrics[endpoint]["ttl_hash"] = ttl_hash
    return metrics


def get_value_of(
    benchmark_run_dao: BenchmarkRunDAO,
    endpoint: Endpoint,
    metric: str,
) -> Optional[float]:
    model_metrics = get_model_metrics(
        benchmark_run_dao,
        endpoint,
        ttl_hash=get_ttl_hash(),
    )
    try:
        value = model_metrics[endpoint.provider][metric]
    except KeyError:
        logger.warning(f"{endpoint} has no metrics. Skipping.")
        return None
    return value


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
        try:
            value = model_metrics[endpoint.provider][metric]
        except KeyError:
            logger.warning(f"{endpoint} has no metrics. Skipping.")
            return math.inf
        return value

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
            value = get_value_of(benchmark_run_dao, endpoint, metric)
            if not value or value >= threshold:
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
    pattern = r"(?P<operator>[<>])(?P<value>\d+\.*\d*)(?P<unit>[\w'-]*)"

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
    if main_metric is None:
        raise invalid_provider_str
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
    else:
        thresholded_endpoints = endpoints
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
