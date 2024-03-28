import pytest
from fastapi import HTTPException

from orchestra.db.dao.benchmark_run_dao import BenchmarkRunDAO
from orchestra.db.dao.endpoint_dao import EndpointDAO
from orchestra.web.api.utils.dynamic_routing import dynamic_routing, parse_endpoint
from orchestra.web.api.utils.http_responses import (
    provider_not_found_under_conditions,
    invalid_provider_str,
)

# TODO: Add test for same value in metric

TEST_VALID_CONFIGS = [
    "lowest-input-cost",
    "lowest-output-cost",
    "lowest-input-cost-per-token",
    "lowest-output-cost-per-token",
    "lowest-itl",
    "itl",
    "highest-tks-per-sec",
    "highest-output-tks-per-sec",
    "lowest-ttft",
    "ttft",
]

TEST_VALID_PRICE_THRESHOLDS = [
    "",
    "<0.1ic",
    "<10ic",
    "<0.1oc",
    "<10oc",
]


@pytest.mark.parametrize("price_threshold", TEST_VALID_PRICE_THRESHOLDS)
@pytest.mark.parametrize("provider", TEST_VALID_CONFIGS)
def test_valid_performance_based_routing(  # type: ignore[return]
    dbsession,
    provider: str,
    price_threshold: str,
) -> str:
    endpoint_dao = EndpointDAO(dbsession)
    benchmark_run_dao = BenchmarkRunDAO(dbsession)

    if "input-cost" in provider and "ic" in price_threshold:
        pytest.skip()
    if "output-cost" in provider and "oc" in price_threshold:
        pytest.skip()

    target_metric, metrics_thresholds = parse_endpoint(provider + price_threshold)
    _, selected_provider = dynamic_routing(
        endpoint_dao,
        benchmark_run_dao,
        target_metric,
        models=("pbr-model",),
        metrics_thresholds=metrics_thresholds,
    )

    expected_provider = {
        "lowest-input-cost": "lowest-input-cost-per-token",
        "lowest-output-cost": "lowest-output-cost-per-token",
        "highest-tks-per-sec": "lowest-itl",
        "highest-output-tks-per-sec": "lowest-itl",
        "itl": "lowest-itl",
        "ttft": "lowest-ttft",
    }.get(provider, provider)

    assert selected_provider == f"{expected_provider + price_threshold}-provider"


def test_new_dynamic_routing(  # type: ignore[return]
    dbsession,
) -> str:
    # model_dao = ModelDAO(dbsession)
    benchmark_run_dao = BenchmarkRunDAO(dbsession)
    # datapoint_dao = DatapointDAO(dbsession)
    endpoint_dao = EndpointDAO(dbsession)

    model, provider = dynamic_routing(
        endpoint_dao,
        benchmark_run_dao,
        # "input_cost_per_token",
        "itl",
        models=("pbr-model",),
        metrics_thresholds={
            # "itl": 750,
            "input_cost_per_token": 10,
        },
    )

    print(model, provider)


def test_empty_lut(dbsession) -> str:  # type: ignore[return]
    endpoint_dao = EndpointDAO(dbsession)
    benchmark_run_dao = BenchmarkRunDAO(dbsession)

    with pytest.raises(HTTPException) as err:
        target_metric, metrics_thresholds = parse_endpoint("lowest-ttft")
        _ = dynamic_routing(
            endpoint_dao,
            benchmark_run_dao,
            target_metric,
            models=("pbr-model-empty-lut",),
            metrics_thresholds=metrics_thresholds,
        )
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
        target_metric, metrics_thresholds = parse_endpoint("lowest-ttft<0.0001ic")
        _ = dynamic_routing(
            endpoint_dao,
            benchmark_run_dao,
            target_metric,
            models=("pbr-model",),
            metrics_thresholds=metrics_thresholds,
        )
    assert err.value.status_code == provider_not_found_under_conditions.status_code
    assert err.value.detail == provider_not_found_under_conditions.detail


def test_invalid_provider(dbsession) -> str:  # type: ignore[return]
    endpoint_dao = EndpointDAO(dbsession)
    benchmark_run_dao = BenchmarkRunDAO(dbsession)

    with pytest.raises(HTTPException) as err:
        target_metric, metrics_thresholds = parse_endpoint("dog")
    assert err.value.status_code == invalid_provider_str.status_code
    assert err.value.detail == invalid_provider_str.detail
