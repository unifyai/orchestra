# TODO: lru cache needs to be changed to a bg task
import logging
import math
import re
import time
from collections import namedtuple
from typing import Dict, List, Optional, Tuple, Union

from google.cloud import aiplatform
from providers.completion import PROVIDER_CLASSES

from orchestra.db.dao.benchmark_run_dao import BenchmarkRunDAO
from orchestra.db.dao.endpoint_dao import EndpointDAO
from orchestra.db.dao.custom_router_dao import CustomRouterDAO
from orchestra.settings import settings

# TODO: Add errors back to the refactored function
from orchestra.web.api.utils.http_responses import (  # invalid_optimisation_goal,; invalid_price_threshold,
    invalid_provider_str,
    provider_not_found_under_conditions,
    server_error_with_digest,
)

logger = logging.getLogger(__name__)

### Arbitrary function

default_models = {
    "claude-3-haiku",
    "claude-3-opus",
    "claude-3-sonnet",
    "deepseek-coder-33b-instruct",
    "gemma-7b-it",
    "gpt-3.5-turbo",
    "gpt-4-turbo",
    "gpt-4o",
    "llama-3-70b-chat",
    "llama-3-8b-chat",
    "mistral-large",
    "mistral-small",
    "mixtral-8x22b-instruct-v0.1",
    "mixtral-8x7b-instruct-v0.1",
}

default_providers = {
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

        self.default_models = default_models
        self.models = self.extract_list("models")

        self.default_providers = default_providers
        self.providers = self.extract_list("providers")

        self.q = self.extract_factor("q")
        self.c = self.extract_factor("c")
        self.i = self.extract_factor("i")
        self.t = self.extract_factor("t")

        self.q0 = self.extract_factor("q0")
        self.c0 = self.extract_factor("c0")
        self.i0 = self.extract_factor("i0")
        self.t0 = self.extract_factor("t0")

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
        return (float("-inf"), float("inf"))

    def cost_fn(self, quality, cost, itl, ttft, **kwargs):
        return (
            -self.q * (quality - self.q0)
            + self.c * (cost - self.c0)
            + self.i * (itl - self.i0)
            + self.t * (ttft - self.t0)
        )

    def __call__(self, prompt, input_tokens, router_endpoint_id, debug=False):
        # Get full list of endpoints
        # endpoints = get_endpoints_of(
        #     self.endpoint_dao,
        #     tuple(self.models),
        #     only_from=tuple(self.providers),
        #     ttl_hash=get_ttl_hash(),
        # )
        endpoints = baked_router_endpoints
        endpoints = [e for e in endpoints if e.provider in self.providers]
        endpoints = [e for e in endpoints if e.model in self.models]
        # Get quality from the neural router scoring function
        model_scores = neural_scoring(prompt, router_endpoint_id)
        if debug:
            return model_scores

        # Load cached metrics
        with open(settings.cache_path) as f:
            endpoint_metrics = json.load(f)
        thresholded_endpoints = []
        # Iterate over each endpoint
        for endpoint in endpoints:
            if endpoint.model not in model_scores:
                continue
            endpoint_metrics[endpoint.id]["quality"] = model_scores[endpoint.model]

            endpoint_metrics[endpoint.id]["cost"] = (
                endpoint_metrics[endpoint.id]["input_cost_per_token"] * 3
                + endpoint_metrics[endpoint.id]["output_cost_per_token"]
            ) / 4

            endpoint_ctx_window = PROVIDER_CLASSES[endpoint.provider](
                "",
            ).supported_models[endpoint.model]["context_window"]

            if endpoint_ctx_window <= input_tokens:
                continue

            # Remove endpoints outside of the thresholds
            for metric, threshold in self.thresholds.items():
                if not(threshold[0] < endpoint_metrics[endpoint.id][metric] < threshold[1]):
                    break
            else:
                thresholded_endpoints.append(endpoint)

        if not thresholded_endpoints:
            raise provider_not_found_under_conditions

        # Compute the cost function for each endpoint
        endpoint_scores = {}
        for endpoint in thresholded_endpoints:
            name = f"{endpoint.model}@{endpoint.provider}"
            endpoint_scores[name] = self.cost_fn(**endpoint_metrics[endpoint.id])

        # Return a list of endpoints ordered by lowest cost
        ordered_keys = sorted(endpoint_scores, key=lambda k: endpoint_scores[k])
        return [key.split("@") for key in ordered_keys]


def neural_scoring(prompt, endpoint_id):
    endpoint = aiplatform.Endpoint(endpoint_id)
    prediction = endpoint.predict(instances=[{"prompt": prompt}])
    out = prediction.predictions[0]["scores"]
    if "gpt-4-0125-preview" in out:
        out["gpt-4-turbo"] = out.pop("gpt-4-0125-preview")
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
        full_hash
        in _cached_endpoints
        # and _cached_endpoints[full_hash].get("ttl_hash", 0) == ttl_hash
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
    # _cached_endpoints[full_hash]["ttl_hash"] = ttl_hash
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
    if f"{endpoint.model}@{endpoint.provider}" in metrics:
        if metric in ["input_cost_per_token", "output_cost_per_token"]:
            metric = "cost"
        return metrics[f"{endpoint.model}@{endpoint.provider}"][metric]
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


def get_router_endpoint_id(
    custom_router_dao: CustomRouterDAO, user_id: str, router_name: str
) -> str:
    ids = custom_router_dao.get_router_id(user_id=user_id, router_name=router_name)
    router_id = ids[0].router_id
    return router_id


baked_router_endpoints = [
    Endpoint(
        id=1299,
        model="mixtral-8x7b-instruct-v0.1",
        model_id=29,
        provider="together-ai",
        provider_id=8,
    ),
    Endpoint(
        id=1300,
        model="mixtral-8x7b-instruct-v0.1",
        model_id=29,
        provider="octoai",
        provider_id=4,
    ),
    Endpoint(
        id=1416,
        model="gpt-4-turbo",
        model_id=135,
        provider="openai",
        provider_id=5,
    ),
    Endpoint(id=1431, model="gpt-4o", model_id=144, provider="openai", provider_id=5),
    Endpoint(
        id=1355,
        model="gpt-3.5-turbo",
        model_id=114,
        provider="openai",
        provider_id=5,
    ),
    Endpoint(
        id=1278,
        model="mixtral-8x7b-instruct-v0.1",
        model_id=29,
        provider="mistral-ai",
        provider_id=3,
    ),
    Endpoint(
        id=1377,
        model="mixtral-8x7b-instruct-v0.1",
        model_id=29,
        provider="anyscale",
        provider_id=2,
    ),
    Endpoint(
        id=1378,
        model="deepseek-coder-33b-instruct",
        model_id=132,
        provider="together-ai",
        provider_id=8,
    ),
    Endpoint(
        id=1387,
        model="mixtral-8x7b-instruct-v0.1",
        model_id=29,
        provider="fireworks-ai",
        provider_id=10,
    ),
    Endpoint(
        id=1401,
        model="mixtral-8x7b-instruct-v0.1",
        model_id=29,
        provider="deepinfra",
        provider_id=12,
    ),
    Endpoint(
        id=1407,
        model="gemma-7b-it",
        model_id=134,
        provider="anyscale",
        provider_id=2,
    ),
    Endpoint(
        id=1408,
        model="gemma-7b-it",
        model_id=134,
        provider="together-ai",
        provider_id=8,
    ),
    Endpoint(
        id=1409,
        model="gemma-7b-it",
        model_id=134,
        provider="fireworks-ai",
        provider_id=10,
    ),
    Endpoint(
        id=1411,
        model="gemma-7b-it",
        model_id=134,
        provider="deepinfra",
        provider_id=12,
    ),
    Endpoint(
        id=1415,
        model="mixtral-8x7b-instruct-v0.1",
        model_id=29,
        provider="aws-bedrock",
        provider_id=13,
    ),
    Endpoint(
        id=1418,
        model="mistral-small",
        model_id=136,
        provider="mistral-ai",
        provider_id=3,
    ),
    Endpoint(
        id=1419,
        model="mistral-large",
        model_id=137,
        provider="mistral-ai",
        provider_id=3,
    ),
    Endpoint(
        id=1420,
        model="claude-3-haiku",
        model_id=138,
        provider="anthropic",
        provider_id=1,
    ),
    Endpoint(
        id=1421,
        model="claude-3-opus",
        model_id=139,
        provider="anthropic",
        provider_id=1,
    ),
    Endpoint(
        id=1422,
        model="claude-3-sonnet",
        model_id=140,
        provider="anthropic",
        provider_id=1,
    ),
    Endpoint(
        id=1423,
        model="mixtral-8x22b-instruct-v0.1",
        model_id=141,
        provider="mistral-ai",
        provider_id=3,
    ),
    Endpoint(
        id=1424,
        model="mixtral-8x22b-instruct-v0.1",
        model_id=141,
        provider="together-ai",
        provider_id=8,
    ),
    Endpoint(
        id=1425,
        model="mixtral-8x22b-instruct-v0.1",
        model_id=141,
        provider="fireworks-ai",
        provider_id=10,
    ),
    Endpoint(
        id=1426,
        model="mixtral-8x22b-instruct-v0.1",
        model_id=141,
        provider="deepinfra",
        provider_id=12,
    ),
    Endpoint(
        id=1427,
        model="llama-3-8b-chat",
        model_id=142,
        provider="together-ai",
        provider_id=8,
    ),
    Endpoint(
        id=1428,
        model="llama-3-8b-chat",
        model_id=142,
        provider="fireworks-ai",
        provider_id=10,
    ),
    Endpoint(
        id=1429,
        model="llama-3-70b-chat",
        model_id=143,
        provider="together-ai",
        provider_id=8,
    ),
    Endpoint(
        id=1430,
        model="llama-3-70b-chat",
        model_id=143,
        provider="fireworks-ai",
        provider_id=10,
    ),
]
