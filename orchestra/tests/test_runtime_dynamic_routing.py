import pytest
from fastapi import HTTPException

from orchestra.db.dao.benchmark_run_dao import BenchmarkRunDAO
from orchestra.db.dao.datapoint_dao import DatapointDAO
from orchestra.db.dao.model_dao import ModelDAO
from orchestra.web.api.utils import performance_based_routing

# TODO: Add test for same value in metric
# TODO: Test response when no result is found (price breakpoints) + add this in the docs

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

TEST_VALID_PRICE_BREAKPOINTS = [
    "",
    "<0.1ic",
    "<10ic",
    "<0.1oc",
    "<10oc",
]


@pytest.mark.parametrize("price_breakpoint", TEST_VALID_PRICE_BREAKPOINTS)
@pytest.mark.parametrize("provider", TEST_VALID_CONFIGS)
def test_valid_performance_based_routing(  # type: ignore[return]
    dbsession,
    provider: str,
    price_breakpoint: str,
) -> str:
    model_dao = ModelDAO(dbsession)
    benchmark_run_dao = BenchmarkRunDAO(dbsession)
    datapoint_dao = DatapointDAO(dbsession)

    if "input-cost" in provider and "ic" in price_breakpoint:
        pytest.skip()
    if "output-cost" in provider and "oc" in price_breakpoint:
        pytest.skip()

    selected_provider = performance_based_routing(
        "pbr-model",
        provider + price_breakpoint,
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

    assert selected_provider == f"{expected_provider + price_breakpoint}-provider"


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

    # TODO: Move this to a shared exceptions file
    e = HTTPException(
        status_code=400,  # noqa: WPS432
        detail=f"Invalid input. model-id doesn't match any entry in the model hub.",
    )

    with pytest.raises(HTTPException) as err:
        performance_based_routing(
            "pbr-model2",
            "lowest-ttft",
            model_dao,
            benchmark_run_dao,
            datapoint_dao,
        )
    assert err.value.status_code == e.status_code
    assert err.value.detail == e.detail


def test_no_models_within_breakpoint(dbsession) -> str:  # type: ignore[return]
    model_dao = ModelDAO(dbsession)
    benchmark_run_dao = BenchmarkRunDAO(dbsession)
    datapoint_dao = DatapointDAO(dbsession)

    # TODO: Move this to a shared exceptions file
    e = HTTPException(
        status_code=404,
        detail="No providers found within the specified price limits.",
    )

    with pytest.raises(HTTPException) as err:
        performance_based_routing(
            "pbr-model",
            "lowest-ttft<0.0001ic",
            model_dao,
            benchmark_run_dao,
            datapoint_dao,
        )
    assert err.value.status_code == e.status_code
    assert err.value.detail == e.detail
