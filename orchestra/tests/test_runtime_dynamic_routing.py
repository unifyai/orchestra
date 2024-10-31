from typing import Any, Dict, Tuple

import pytest
from fastapi import HTTPException

from orchestra.db.dao.benchmark_run_dao import BenchmarkRunDAO
from orchestra.db.dao.endpoint_dao import EndpointDAO
from orchestra.web.api.utils.dynamic_routing import Router
from orchestra.web.api.utils.http_responses import (
    invalid_provider_str,
    provider_not_found_under_conditions,
)

# TODO: Add test for same value in metric

TEST_CASES = [
    (
        "input-cost",
        {
            "metric": "input-cost",
            "key": "keyword",
            "value": "lowest",
        },
    ),
    (
        "oc>10",
        {
            "metric": "output-cost",
            "key": "checks",
            "value": [
                {
                    "threshold": 10,
                    "op": ">",
                },
            ],
        },
    ),
    (
        "highest-i",
        {
            "metric": "itl",
            "key": "keyword",
            "value": "highest",
        },
    ),
    (
        "ttft>10|ttft<30",
        {
            "metric": "ttft",
            "key": "checks",
            "value": [
                {
                    "threshold": 10,
                    "op": ">",
                },
                {
                    "threshold": 30,
                    "op": "<",
                },
            ],
        },
    ),
]


@pytest.mark.parametrize("test_case", TEST_CASES)
def test_valid_performance_based_routing(  # type: ignore[return]
    dbsession,
    test_case: Tuple[str, Dict[str, Any]],
) -> str:
    endpoint_dao = EndpointDAO(dbsession)
    benchmark_run_dao = BenchmarkRunDAO(dbsession)
    provider, threshold = test_case

    metrics_and_thresholds = Router(
        f"claude-3.5-sonnet@{provider}",
        "",
        endpoint_dao,
        benchmark_run_dao,
    ).metrics_and_thresholds

    metric = threshold["metric"]
    if threshold["key"] == "checks":
        for i, check in enumerate(metrics_and_thresholds[metric]["checks"]):
            assert check["threshold"] == threshold["value"][i]["threshold"]
            assert check["op"] == threshold["value"][i]["op"]
    else:
        assert metrics_and_thresholds[metric]["keyword"] == threshold["value"]


def test_new_dynamic_routing(  # type: ignore[return]
    dbsession,
) -> str:
    # model_dao = ModelDAO(dbsession)
    benchmark_run_dao = BenchmarkRunDAO(dbsession)
    # datapoint_dao = DatapointDAO(dbsession)
    endpoint_dao = EndpointDAO(dbsession)

    model, provider, _ = Router(
        "llama-3.1-8b-chat@quality|input-cost<=0.8|output-cost<=0.8|itl>1|itl<20",
        "",
        endpoint_dao,
        benchmark_run_dao,
    )()

    print(model, provider)


def test_empty_lut(dbsession) -> str:  # type: ignore[return]
    endpoint_dao = EndpointDAO(dbsession)
    benchmark_run_dao = BenchmarkRunDAO(dbsession)

    with pytest.raises(HTTPException) as err:
        Router(
            "pbr-model-empty-lut@ttft",
            "",
            endpoint_dao,
            benchmark_run_dao,
        )()
    assert err.value.status_code == 500


# TODO: This needs to happen at chat/completions and inference level
# def test_incorrect_model_id(dbsession) -> str:  # type: ignore[return]
#     endpoint_dao = EndpointDAO(dbsession)
#     benchmark_run_dao = BenchmarkRunDAO(dbsession)
#
#     with pytest.raises(HTTPException) as err:
#         target_metric, metrics_thresholds = parse_endpoint("lowest-ttft")
#         _ = dynamic_routing(
#             endpoint_dao,
#             benchmark_run_dao,
#             target_metric,
#             models=("pbr-model2",),
#             metrics_thresholds=metrics_thresholds,
#         )
#     assert err.value.status_code == invalid_model_id.status_code
#     assert err.value.detail == invalid_model_id.detail


def test_no_models_within_threshold(dbsession) -> str:  # type: ignore[return]
    endpoint_dao = EndpointDAO(dbsession)
    benchmark_run_dao = BenchmarkRunDAO(dbsession)

    with pytest.raises(HTTPException) as err:
        Router(
            "llama-3.1-8b-chat@itl<1|itl<20",
            "",
            endpoint_dao,
            benchmark_run_dao,
        )()
    assert err.value.status_code == provider_not_found_under_conditions.status_code
    assert err.value.detail == provider_not_found_under_conditions.detail


def test_invalid_provider(dbsession) -> str:  # type: ignore[return]
    endpoint_dao = EndpointDAO(dbsession)
    benchmark_run_dao = BenchmarkRunDAO(dbsession)

    with pytest.raises(HTTPException) as err:
        Router(
            "llama-3.1-8b-chat@itr>1|itr<20",
            "",
            endpoint_dao,
            benchmark_run_dao,
        )()
    assert err.value.status_code == invalid_provider_str.status_code
    assert err.value.detail == invalid_provider_str.detail


if __name__ == "__main__":
    pass
