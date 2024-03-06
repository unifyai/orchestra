import pytest
from fastapi import HTTPException

from orchestra.db.dao.benchmark_run_dao import BenchmarkRunDAO
from orchestra.db.dao.datapoint_dao import DatapointDAO
from orchestra.db.dao.model_dao import ModelDAO
from orchestra.web.api.utils.dynamic_routing import performance_based_routing
from orchestra.web.api.utils.http_responses import (
    invalid_model_id,
    provider_not_found_under_conditions,
)

# TODO: Add test for same value in metric

TEST_VALID_CONFIGS = [
    "lowest-input-cost",
    "lowest-output-cost",
    "lowest-input-cost-per-token",
    "lowest-output-cost-per-token",
    "lowest-itl",
    "highest-tks-per-sec",
    "highest-output-tks-per-sec",
    "lowest-ttft",
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
    model_dao = ModelDAO(dbsession)
    benchmark_run_dao = BenchmarkRunDAO(dbsession)
    datapoint_dao = DatapointDAO(dbsession)

    if "input-cost" in provider and "ic" in price_threshold:
        pytest.skip()
    if "output-cost" in provider and "oc" in price_threshold:
        pytest.skip()

    selected_provider = performance_based_routing(
        "pbr-model",
        provider + price_threshold,
        model_dao,
        benchmark_run_dao,
        datapoint_dao,
    )

    expected_provider = {
        "lowest-input-cost": "lowest-input-cost-per-token",
        "lowest-output-cost": "lowest-output-cost-per-token",
        "highest-tks-per-sec": "lowest-itl",
        "highest-output-tks-per-sec": "lowest-itl",
    }.get(provider, provider)

    assert selected_provider == f"{expected_provider + price_threshold}-provider"


def test_empty_lut(dbsession) -> str:  # type: ignore[return]
    model_dao = ModelDAO(dbsession)
    benchmark_run_dao = BenchmarkRunDAO(dbsession)
    datapoint_dao = DatapointDAO(dbsession)

    with pytest.raises(HTTPException) as err:
        performance_based_routing(
            "pbr-model-empty-lut",
            "lowest-ttft",
            model_dao,
            benchmark_run_dao,
            datapoint_dao,
        )
    assert err.value.status_code == 500


def test_incorrect_model_id(dbsession) -> str:  # type: ignore[return]
    model_dao = ModelDAO(dbsession)
    benchmark_run_dao = BenchmarkRunDAO(dbsession)
    datapoint_dao = DatapointDAO(dbsession)

    with pytest.raises(HTTPException) as err:
        performance_based_routing(
            "pbr-model2",
            "lowest-ttft",
            model_dao,
            benchmark_run_dao,
            datapoint_dao,
        )
    assert err.value.status_code == invalid_model_id.status_code
    assert err.value.detail == invalid_model_id.detail


def test_no_models_within_threshold(dbsession) -> str:  # type: ignore[return]
    model_dao = ModelDAO(dbsession)
    benchmark_run_dao = BenchmarkRunDAO(dbsession)
    datapoint_dao = DatapointDAO(dbsession)

    with pytest.raises(HTTPException) as err:
        performance_based_routing(
            "pbr-model",
            "lowest-ttft<0.0001ic",
            model_dao,
            benchmark_run_dao,
            datapoint_dao,
        )
    assert err.value.status_code == provider_not_found_under_conditions.status_code
    assert err.value.detail == provider_not_found_under_conditions.detail
