# TODO: lru cache needs to be changed to a bg task
import logging
import math
import os
import re
import time
from collections import namedtuple
from typing import Dict, List, Optional, Tuple, Union

import requests
from fastapi import Request
from google.cloud import aiplatform
from providers.completion import PROVIDER_CLASSES

from orchestra.db.dao.benchmark_run_dao import BenchmarkRunDAO
from orchestra.db.dao.custom_router_dao import CustomRouterDAO
from orchestra.db.dao.endpoint_dao import EndpointDAO

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
    "fireworks-ai",
    "deepinfra",
    "octoai",
    "aws-bedrock",
}


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


class Router:
    def __init__(self, model, endpoint_dao, benchmark_run_dao):
        self.model = model
        self.endpoint_dao = endpoint_dao
        self.benchmark_run_dao = benchmark_run_dao
        self.metric_aliases = [
            ["quality", "q"],
            ["ttft", "time-to-first-token", "t"],
            ["itl", "inter-token-latency", "i"],
            ["cost", "c"],
            ["input-cost", "ic"],
            ["output-cost", "oc"],
        ]
        self.metric_map = dict()
        self.metrics_and_thresholds = dict()
        self.using_obj_fn = False
        self.models = None
        self.skip_models = None
        self.providers = None
        self.skip_providers = None
        self.load_metric_map()
        self.load_metrics_and_thresholds()
        self.load_models_and_providers()
        self.model = self.model.split("@")[0]

    def load_metric_map(self):
        # create a dict for mapping each alias to a common term
        for metric_alias in self.metric_aliases:
            for metric in metric_alias:
                self.metric_map[metric] = metric_alias[0]

    def load_metrics_and_thresholds(self):
        # get the string representing the constraints
        provider_str = self.model.split("@")[1]
        metrics = provider_str.split("|")

        for metric in metrics:
            # get the name of the metric
            metric_name = metric.split("<", 1)[0].split(">")[0].split(":")[0]
            main_metric = self.metric_map.get(
                metric_name.replace("lowest-", "").replace("highest-", ""),
            )
            if main_metric is None:
                continue

            # get the keywords specified in the string without any numbers
            # when numbers are specified then these get considered as "none"
            keywords = re.findall("(lowest|highest)-", metric_name)
            keyword = (
                keywords[0]
                if len(keywords)
                else ("highest" if main_metric == "quality" else "lowest")
            )

            # get all number-based constraints of the metrics
            thresholds = re.findall(r"[<>]=?\d+\.?\d*", metric)
            checks = []
            for threshold in thresholds:
                # get the value
                val = float(re.findall(r"\d+\.?\d*", threshold)[0])

                # get the comparison sign
                op = re.findall(r"[<>]=?", threshold)[0]
                cmp = None
                if op == ">=":
                    cmp = lambda value, threshold: value >= threshold
                elif op == ">":
                    cmp = lambda value, threshold: value > threshold
                elif op == "<=":
                    cmp = lambda value, threshold: value <= threshold
                else:
                    cmp = lambda value, threshold: value < threshold

                # add the check to the list
                checks.append({"threshold": val, "op": op, "cmp": cmp})
                keyword = self.metrics_and_thresholds.get(main_metric, dict()).get(
                    "keyword",
                    "none",
                )

            # get the multiplers specified with :
            multipliers = [
                float(factor[1:]) for factor in re.findall(r":\d+\.?\d*", metric)
            ]
            if len(multipliers):
                self.using_obj_fn = True

            # store all the info, concatenate with previous checks if multiple
            # checks are specified for each metric
            self.metrics_and_thresholds[main_metric] = {
                "keyword": keyword,
                "checks": (
                    self.metrics_and_thresholds.get(main_metric, {"checks": []})[
                        "checks"
                    ]
                    + checks
                ),
                "multipliers": multipliers,
            }

        # if we're using multipliers then we need to track another metric
        # for all non-specified constraints, the multiplier is set to 0
        if self.using_obj_fn:
            for metric in self.metrics_and_thresholds:
                multipliers = self.metrics_and_thresholds[metric]["multipliers"]
                if len(multipliers) == 0:
                    multipliers.append(0)

    def load_models_and_providers(self):
        # get the model and provider specifications
        search_str = self.model.split("@")[1]
        for criteria_str in search_str.split("|"):
            if ":" not in criteria_str:
                continue
            criteria, criteria_val = criteria_str.split(":")
            if criteria in ["providers", "skip_providers", "models", "skip_models"]:
                criteria_list = criteria_val.split(",")
                if criteria == "providers":
                    self.providers = criteria_list
                elif criteria == "skip_providers":
                    self.skip_providers = criteria_list
                elif criteria == "models":
                    self.models = criteria_list
                else:
                    self.skip_models = criteria_list

    def get_public_endpoint_metrics(self, endpoint: Endpoint, request_fastapi: Request):
        # query the public endpoint on-prem to get benchmarks
        model = endpoint.model
        provider = endpoint.provider
        request_url = os.environ.get("PUBLIC_ORCHESTRA_URL", "") + "/benchmark"
        kwargs = {"model": model, "provider": provider}
        headers = {
            key: value
            for key, value in request_fastapi._headers.items()
            if key in ["content-type", "authorization"]
        }
        return requests.get(
            request_url,
            params=kwargs,
            headers=headers,
        ).json()

    def get_metric_value(
        self,
        endpoint_metrics: Dict[str, Dict[str, float]],
        endpoint: Endpoint,
        metric: str,
        scores: Optional[Dict[str, float]] = None,
    ) -> float:
        try:
            return endpoint_metrics[endpoint.model + "@" + endpoint.provider][
                metric.replace("-", "_")
            ]
        except KeyError:
            # skip quality-based filtering unless performing
            # neural routing
            if scores is not None or metric != "quality":
                logger.warning(
                    f"{endpoint} has no metric {metric}. Skipping.",
                )
                return math.inf

        # get value for the metric on-prem
        if os.environ.get("ON_PREM"):
            try:
                return endpoint_metrics[endpoint.model + "@" + endpoint.provider][
                    metric.replace("-", "_")
                ]
            except KeyError:
                logger.warning(f"{endpoint} has no metrics. Skipping.")
                return math.inf

        # get the value on the cloud deployment
        model_metrics = get_model_metrics(
            self.benchmark_run_dao,
            endpoint,
            ttl_hash=get_ttl_hash(),
        )
        try:
            value = model_metrics[endpoint.provider][metric]
        except KeyError:
            logger.warning(f"{endpoint} has no metrics. Skipping.")
            return math.inf
        return value

    def obj_fn(self, metrics):
        objective = 0
        for metric in self.metrics_and_thresholds:
            if metric in metrics:
                value = (
                    metrics[metric]
                    * self.metrics_and_thresholds[metric]["multipliers"][0]
                )
                if metric == "quality":
                    value = -value
                objective += value
        return objective

    def __call__(
        self,
        request_fastapi: Request,
        endpoints: Optional[List[Endpoint]] = None,
        scores: Optional[Dict[str, float]] = None,
        input_tokens: Optional[int] = None,
        return_all: bool = False,
    ):
        # get all endpoints from the specified models x providers.
        # this is skipped in case of the neural scoring function
        if endpoints is None:
            endpoints = get_endpoints_of(
                self.endpoint_dao,
                (self.model,),
                only_from=tuple(self.providers) if self.providers else None,
                ttl_hash=get_ttl_hash(),
            )
            if self.skip_providers:
                endpoints = [
                    endpoint
                    for endpoint in endpoints
                    if endpoint.provider not in self.skip_providers
                ]

        # remove endpoints that don't fall within the metrics thresholds
        endpoint_metrics = dict()
        if len(self.metrics_and_thresholds.keys()):
            thresholded_endpoints = []
            for endpoint in endpoints:
                is_valid = True

                # get metrics for the endpoint on-prem
                if os.environ.get("ON_PREM"):
                    metrics = self.get_public_endpoint_metrics(
                        endpoint,
                        request_fastapi,
                    )
                    if isinstance(metrics, list):
                        metrics = metrics[0]
                    else:
                        metrics = dict()
                else:
                    metrics = get_model_metrics(
                        self.benchmark_run_dao,
                        endpoint,
                        ttl_hash=get_ttl_hash(),
                    )[endpoint.provider]

                # store the cost with the 3:1 ratio
                metrics["cost"] = (
                    3 * metrics["input_cost"] + metrics["output_cost"]
                ) / 4
                if scores:
                    metrics["quality"] = scores[endpoint.model]
                endpoint_metrics[endpoint.model + "@" + endpoint.provider] = metrics

                # iterate over metrics and thresholds for filtering
                for metric, data in self.metrics_and_thresholds.items():
                    # get model metrics
                    value = self.get_metric_value(
                        endpoint_metrics,
                        endpoint,
                        metric,
                        scores,
                    )
                    if value == math.inf:
                        is_valid = False
                        break

                    # store the context window size for the endpoint
                    context_window = PROVIDER_CLASSES[endpoint.provider](
                        "",
                    ).supported_models[endpoint.model]["context_window"]
                    if input_tokens and context_window <= input_tokens:
                        is_valid = False

                    # check for the threshold
                    for check in data["checks"]:
                        if not value or (
                            "cmp" in check
                            and not check["cmp"](value, check["threshold"])
                        ):
                            is_valid = False
                            break

                # add the valid endpoints to the list
                if is_valid:
                    thresholded_endpoints.append(endpoint)

            # if no endpoints found then return error
            if not thresholded_endpoints:
                raise provider_not_found_under_conditions

            if self.using_obj_fn:
                # compute the objective function value for all the endpoints
                for endpoint in thresholded_endpoints:
                    endpoint_metrics[endpoint.model + "@" + endpoint.provider][
                        "objective"
                    ] = self.obj_fn(
                        endpoint_metrics[endpoint.model + "@" + endpoint.provider],
                    )

                # sort based on the objective function
                sorted_endpoints = sorted(
                    thresholded_endpoints,
                    key=lambda endpoint: endpoint_metrics[
                        endpoint.model + "@" + endpoint.provider
                    ]["objective"],
                )
            else:
                # sort the thresholded endpoints
                sorted_endpoints = thresholded_endpoints
                for metric in self.metrics_and_thresholds:
                    if "keyword" in self.metrics_and_thresholds[metric]:
                        keyword = self.metrics_and_thresholds[metric]["keyword"]
                        if keyword != "none":
                            sorted_endpoints = sorted(
                                sorted_endpoints,
                                key=lambda endpoint: self.get_metric_value(
                                    endpoint_metrics,
                                    endpoint,
                                    metric,
                                ),
                                reverse=keyword == "highest",
                            )
            final_endpoints = sorted_endpoints
        else:
            final_endpoints = endpoints

        # return all endpoints if we're routing
        if return_all:
            return [
                (endpoint.model, endpoint.provider, None)
                for endpoint in final_endpoints
            ]
        selected_endpoint = final_endpoints[0]

        return selected_endpoint.model, selected_endpoint.provider, None


class NeuralRouter(Router):
    def __call__(
        self,
        request_fastapi: Request,
        prompt: Optional[str] = "",
        router_endpoint_id: Optional[int] = None,
        input_tokens: Optional[int] = None,
        debug: Optional[bool] = None,
    ):
        endpoints = baked_router_endpoints
        endpoints = [
            e
            for e in endpoints
            if (
                (not self.providers or e.provider in self.providers)
                and (not self.skip_providers or e.provider not in self.skip_providers)
                and (not self.models or e.model in self.models)
                and (not self.skip_models or e.model not in self.skip_models)
            )
        ]
        model_scores = neural_scoring(prompt, router_endpoint_id)
        if debug:
            return model_scores
        return super().__call__(
            request_fastapi=request_fastapi,
            endpoints=endpoints,
            scores=model_scores,
            input_tokens=input_tokens,
            return_all=True,
        )


def get_router_endpoint_id(
    custom_router_dao: CustomRouterDAO,
    user_id: str,
    router_name: str,
) -> str:
    ids = custom_router_dao.get_router_id(user_id=user_id, router_name=router_name)
    router_id = ids[0].router_id
    return router_id


metrics = {
    "claude-3-haiku@anthropic": {
        "cost": 1.25,
        "ttft": 641.8530669999427,
        "itl": 7.320965663317298,
    },
    "claude-3-opus@anthropic": {
        "cost": 75,
        "ttft": 2591.2904499998604,
        "itl": 34.60999395530718,
    },
    "claude-3-sonnet@anthropic": {
        "cost": 15,
        "ttft": 1151.3890589999392,
        "itl": 12.03211326126186,
    },
    "gpt-3.5-turbo@openai": {
        "cost": 1.5,
        "ttft": 400.21933599996373,
        "itl": 27.26199600000041,
    },
    "gpt-4-turbo@openai": {
        "cost": 30,
        "ttft": 635.7509760000539,
        "itl": 42.31438732535859,
    },
    "gpt-4@openai": {
        "cost": 45,
        "ttft": 760,
        "itl": 46.05,
    },
    "gpt-4o@openai": {
        "cost": 7.5,
        "ttft": 589,
        "itl": 20.05,
    },
    "llama-3-70b-chat@fireworks-ai": {"cost": 0.9, "ttft": 469.78, "itl": 6.58},
    "llama-3-70b-chat@together-ai": {"cost": 0.9, "ttft": 466.28, "itl": 5.38},
    "llama-3-8b-chat@fireworks-ai": {"cost": 0.2, "ttft": 355.48, "itl": 3.06},
    "llama-3-8b-chat@together-ai": {"cost": 0.2, "ttft": 1035.13, "itl": 3.98},
    "mistral-large@mistral-ai": {
        "cost": 24,
        "ttft": 439.49507400009225,
        "itl": 54.14005861235942,
    },
    "mistral-small@mistral-ai": {
        "cost": 6,
        "ttft": 371.52690400000665,
        "itl": 18.000006300000376,
    },
    "mixtral-8x7b-instruct-v0.1@together-ai": {
        "cost": 0.6,
        "ttft": 405.11531099997455,
        "itl": 4.174361656626742,
    },
    "mixtral-8x7b-instruct-v0.1@octoai": {
        "cost": 0.5,
        "ttft": 1164.472783000008,
        "itl": 24.274311994623353,
    },
    "mixtral-8x7b-instruct-v0.1@replicate": {
        "cost": 1,
        "ttft": 887.903352999956,
        "itl": 15.394309863636439,
    },
    "mixtral-8x7b-instruct-v0.1@mistral-ai": {
        "cost": 0.7,
        "ttft": 352.0689869999387,
        "itl": 12.773902387097081,
    },
    "mixtral-8x7b-instruct-v0.1@fireworks-ai": {
        "cost": 0.5,
        "ttft": 324.21352400001524,
        "itl": 3.380061226190194,
    },
    "mixtral-8x7b-instruct-v0.1@lepton-ai": {
        "cost": 0.5,
        "ttft": 872.5847029999159,
        "itl": 12.631626471590804,
    },
    "mixtral-8x7b-instruct-v0.1@deepinfra": {
        "cost": 0.27,
        "ttft": 1130.8457239999825,
        "itl": 15.669842747059405,
    },
    "mixtral-8x7b-instruct-v0.1@aws-bedrock": {
        "cost": 0.7,
        "ttft": 713.9613250001275,
        "itl": 15.034942066296473,
    },
    "mixtral-8x22b-instruct-v0.1@mistral-ai": {
        "cost": 3,
        "ttft": 135,
        "itl": 12.25,
    },
    "mixtral-8x22b-instruct-v0.1@fireworks-ai": {
        "cost": 0.9,
        "ttft": 314,
        "itl": 11.63,
    },
    "mixtral-8x22b-instruct-v0.1@together-ai": {
        "cost": 1.2,
        "ttft": 840,
        "itl": 21.88,
    },
}

for endpoint in metrics:
    model, provider = endpoint.split("@")
    metrics[endpoint]["context_window"] = PROVIDER_CLASSES[provider](
        "",
    ).supported_models[model]["context_window"]

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
