import logging
import time
from typing import Any, Dict

from orchestra.web.api.utils.http_responses import (
    invalid_model_id,
    invalid_optimisation_goal,
    invalid_price_threshold,
    provider_not_found_under_conditions,
    server_error_with_digest,
)

logger = logging.getLogger(__name__)

"""
This LUT acts as a cache for performance based routing. The structure is:
{
    "<model-id>": {
        "ts": timestamp of the last update of the metrics, time.time()
        "metrics": {
            "<provider>": {
                "<metric-name>": metric.value
            }
        }
        "lowest": {
            "<metric-name>": {
                "[float]ic": {
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
    logger.info("Refreshed metric in performance LUT.")
    return providers


def update_performance_lut(model, model_dao, benchmark_run_dao, datapoint_dao):
    if model in _performance_lut and _lt_hours(_performance_lut[model]["ts"]):
        return
    try:
        model_id = model_dao.filter(mdl_code=model)[0].id
    except IndexError:
        raise invalid_model_id
    # This removes all the cached price thresholds (on purpose)
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


def valid_price_threshold(price_threshold):
    if price_threshold == "inf<>":
        return True
    if price_threshold[-2:] not in ["ic", "oc"]:
        return False
    try:
        float(price_threshold[:-2])
    except ValueError:
        return False
    return True


def _compute_lowest(model, criterium, metric, price_threshold):
    metrics_dict = _performance_lut[model]["metrics"]
    # TODO: Add support for this
    comparation_fn = {
        "highest": max,
        "lowest": min,
    }[criterium]
    brkp_value = float(price_threshold[:-2])
    brkp_metric = {
        "<>": None,
        "ic": "input_cost_per_token",
        "oc": "output_cost_per_token",
    }[price_threshold[-2:]]

    optimal_metric = {}
    # Iterate over each provider
    for provider, provider_metrics in metrics_dict.items():
        value = provider_metrics[metric]
        if brkp_metric and provider_metrics[brkp_metric] >= brkp_value:
            continue
        if not optimal_metric or value < optimal_metric["value"]:
            optimal_metric = {"provider": provider, "value": value}
    if not optimal_metric:
        if brkp_metric:
            raise provider_not_found_under_conditions
        else:
            debug_info = {
                "model": model,
                "metric": metric,
                "price_threshold": price_threshold,
            }
            server_error, digest = server_error_with_digest(str(debug_info))
            logger.error(f"Digest {digest}: {str(debug_info)}")
            raise server_error
    logger.info(f"Added ({model}, {metric}, {price_threshold}) to performance LUT.")
    return optimal_metric["provider"]


def _get_provider_from_lut(model, criterium, metric, price_threshold):
    if criterium not in _performance_lut[model]:
        _performance_lut[model][criterium] = {}
    if metric not in _performance_lut[model][criterium]:
        _performance_lut[model][criterium][metric] = {}
    if price_threshold not in _performance_lut[model][criterium][metric]:
        _performance_lut[model][criterium][metric][price_threshold] = _compute_lowest(
            model,
            criterium,
            metric,
            price_threshold,
        )
    return _performance_lut[model][criterium][metric][price_threshold]


def performance_based_routing(
    model,
    provider: str,
    model_dao,
    benchmark_run_dao,
    datapoint_dao,
):
    price_threshold = "inf<>"
    if ">" in provider:
        raise invalid_price_threshold
    if "<" in provider:
        provider, price_threshold = provider.split("<", 1)
    if not valid_price_threshold(price_threshold):
        raise invalid_price_threshold
    if provider not in performance_rules:
        raise invalid_optimisation_goal(performance_rules)

    # refresh metrics if needed
    update_performance_lut(model, model_dao, benchmark_run_dao, datapoint_dao)

    provider = _aliases(provider)
    criterium, metric = provider.split("-", 1)
    metric = metric.replace("-", "_")
    ret = _get_provider_from_lut(model, criterium, metric, price_threshold)
    return ret
