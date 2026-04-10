import json
import statistics
import uuid
from datetime import datetime, timedelta
from typing import List, Optional

import numpy as np
import pytest
from httpx import AsyncClient

from ...web.api.log.utils.type_utils import normalize_timestamp
from . import (
    HEADERS,
    _create_derived_entry,
    _create_log,
    _create_project,
    _create_several_logs,
    log_data,
)

# Reduction #
# ----------#


def _is_timestamp(v: any):
    try:
        normalized = normalize_timestamp(v)
        datetime.fromisoformat(normalized)
        return True
    except:
        return False


def _is_type_for_len(v: any) -> bool:
    return (
        (isinstance(v, str) and not _is_timestamp(v))
        or isinstance(v, list)
        or isinstance(v, dict)
        or isinstance(v, tuple)
        or isinstance(v, set)
    )


def _is_all_unique(vals):
    """
    Check if all entries in vals are unique. Works even for unhashable types like lists or dicts.
    """
    seen = []
    for val in vals:
        if val in seen:
            return False
        seen.append(val)
    return True


def _preprocess(
    values: list,
) -> tuple:
    assert all(
        isinstance(x, type(values[0])) for x in values
    ), "Not all elements have the same type"
    if _is_type_for_len(values[0]):
        return [len(v) for v in values], False
    elif _is_timestamp(values[0]):
        return [datetime.fromisoformat(v).timestamp() for v in values], True
    else:
        return values, False


def _count(values: list) -> any:
    values, _ = _preprocess(values)
    return len(values)


def _sum(values: list) -> any:
    values, is_timestamp = _preprocess(values)
    ret = sum(values)
    return datetime.fromtimestamp(ret).isoformat() if is_timestamp else ret


def _mean(values: list) -> any:
    values, is_timestamp = _preprocess(values)
    ret = sum(values) / len(values)
    return datetime.fromtimestamp(ret).isoformat() if is_timestamp else ret


def _var(values: list) -> any:
    values, is_timestamp = _preprocess(values)
    num_values = len(values)
    mean_val = sum(values) / num_values
    diffs_squared = [(v - mean_val) ** 2 for v in values]
    ret = sum(diffs_squared) / num_values
    return timedelta(seconds=ret).__repr__() if is_timestamp else ret


def _std(values: list) -> any:
    values, is_timestamp = _preprocess(values)
    num_values = len(values)
    mean_val = sum(values) / num_values
    diffs_squared = [(v - mean_val) ** 2 for v in values]
    ret = (sum(diffs_squared) / num_values) ** 0.5
    return timedelta(seconds=ret).__repr__() if is_timestamp else ret


def _min(values: list) -> any:
    values, is_timestamp = _preprocess(values)
    ret = min(values)
    return datetime.fromtimestamp(ret).isoformat() if is_timestamp else ret


def _max(values: list) -> any:
    values, is_timestamp = _preprocess(values)
    ret = max(values)
    return datetime.fromtimestamp(ret).isoformat() if is_timestamp else ret


def _median(values: list) -> any:
    values, is_timestamp = _preprocess(values)
    ret = statistics.median(values)
    return datetime.fromtimestamp(ret).isoformat() if is_timestamp else ret


def _mode(values: list) -> any:
    values, is_timestamp = _preprocess(values)
    ret = statistics.mode(values)
    return datetime.fromtimestamp(ret).isoformat() if is_timestamp else ret


reduction_methods = {
    "count": _count,
    "sum": _sum,
    "mean": _mean,
    "var": _var,
    "std": _std,
    "min": _min,
    "max": _max,
    "median": _median,
    "mode": _mode,
}


@pytest.mark.anyio
@pytest.mark.parametrize(
    "key",
    [
        "_/description",
        "_/temperature",
        "_/safe",
        "_/metadata",
        "_/_data",
        "_/timestamp",
        "temp_plus_10",  # Derived: temp + 10
        "desc_len",  # Derived: len(description)
    ],
)
@pytest.mark.parametrize(
    "metric",
    ["sum", "mean", "var", "std", "min", "max", "median", "mode"],
)
@pytest.mark.parametrize(
    "from_ids",
    [[1, 3, 5, 6], None],
)
async def test_get_logs_metric(
    client: AsyncClient,
    key: str,
    metric: str,
    from_ids: Optional[List[int]],
):
    project_name = "eval-project"
    _ = await _create_project(client, project_name)
    _ = await _create_several_logs(client, project_name)
    data = log_data["logs_for_various"]
    derived_data = []
    # Create derived logs if needed
    if key == "temp_plus_10":
        config = {
            "key": "temp_plus_10",
            "equation": "{t:_/temperature} + 10",
            "referenced_logs": {"t": [1, 2, 3, 4]},
        }
        response = await _create_derived_entry(
            client,
            project_name,
            config["key"],
            config["equation"],
            config["referenced_logs"],
        )
        assert response.status_code == 200
        # Patch local data so test can reuse the same metric code:
        for i in range(4):
            if "_/temperature" in data[i]:
                derived_data.append(data[i]["_/temperature"] + 10)

    elif key == "desc_len":
        config = {
            "key": "desc_len",
            "equation": "len({d:_/description})",
            "referenced_logs": {"d": [1, 2, 3, 4, 5, 6]},
        }
        response = await _create_derived_entry(
            client,
            project_name,
            config["key"],
            config["equation"],
            config["referenced_logs"],
        )
        assert response.status_code == 200
        for i in range(len(data)):
            if "_/description" in data[i]:
                derived_data.append(len(data[i]["_/description"]))

    params = (
        {"key": key}
        if from_ids is None or key in ("temp_plus_10", "desc_len")
        else {"key": key, "from_ids": "&".join([str(i) for i in from_ids])}
    )
    response = await client.get(
        f"/v0/logs/metric/{metric}?project_name={project_name}",
        params=params,
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    if key in ("temp_plus_10", "desc_len"):
        vals = derived_data
    else:
        vals = [
            d[key]
            for i, d in enumerate(data)
            if key in d and (from_ids is None or i + 1 in from_ids)
        ]
    if metric == "mode" and _is_all_unique(vals):
        # early return to avoid computing 'mode' which is order-dependent
        # in case of unique entries.
        return
    correct = reduction_methods[metric](vals)
    if isinstance(correct, str):
        # ignore milliseconds, as tiny rounding float differences can occur
        assert result.split(".")[0] == correct.split(".")[0]
    else:
        assert np.isclose(result, correct, atol=1e-6)


@pytest.mark.anyio
async def test_get_logs_metric_batch(client: AsyncClient):
    """Test the batch processing functionality of the get_logs_metric endpoint."""
    # 1. Create a test project and insert logs
    project_name = "eval-project-batch"
    _ = await _create_project(client, project_name)
    _ = await _create_several_logs(client, project_name)
    data = log_data["logs_for_various"]

    # 2. Create derived logs for testing
    #    First derived log: temperature + 10
    derived_conf_temp = {
        "key": "temp_plus_10",
        "equation": "{t:_/temperature} + 10",
        "referenced_logs": {"t": [1, 2, 3, 4]},  # logs that have a _/temperature
    }
    response = await _create_derived_entry(
        client,
        project_name,
        derived_conf_temp["key"],
        derived_conf_temp["equation"],
        derived_conf_temp["referenced_logs"],
    )
    assert response.status_code == 200, response.json()

    #    Second derived log: length of description
    derived_conf_desc = {
        "key": "desc_len",
        "equation": "len({d:_/description})",
        "referenced_logs": {"d": [1, 2, 3, 4, 5, 6]},
    }
    response = await _create_derived_entry(
        client,
        project_name,
        derived_conf_desc["key"],
        derived_conf_desc["equation"],
        derived_conf_desc["referenced_logs"],
    )
    assert response.status_code == 200, response.json()

    #
    # 3. Test single-key usage (legacy) to ensure backward compatibility
    #
    single_key = "_/temperature"
    resp = await client.get(
        f"/v0/logs/metric/mean?project_name={project_name}&key={single_key}",
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    single_result = resp.json()
    # Should be a scalar (float, int, etc.), not a dict
    assert isinstance(
        single_result,
        (int, float, str),
    ), "Expected scalar result for single key usage."

    #
    # 4. Test multiple-key usage
    #
    multiple_keys = ["_/temperature", "_/safe", "temp_plus_10", "desc_len"]
    resp = await client.get(
        f"/v0/logs/metric/mean?project_name={project_name}",
        params={"key": json.dumps(multiple_keys)},
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    multi_result = resp.json()
    assert isinstance(
        multi_result,
        dict,
    ), "Expected dict result for multiple-key usage."
    assert set(multi_result.keys()) == set(
        multiple_keys,
    ), f"Expected keys {multiple_keys}, got {multi_result.keys()}"

    #
    # 5. Key-specific filter expressions
    #    Example: _/temperature > 0, and _/safe == true
    #
    filter_expr_dict = {
        "_/temperature": "_/temperature > 0",  # only positive temps
        "_/safe": "_/safe == 'true'",  # only logs with safe == true
    }
    resp = await client.get(
        f"/v0/logs/metric/mean?project_name={project_name}",
        params={
            "key": json.dumps(["_/temperature", "_/safe"]),
            "filter_expr": json.dumps(filter_expr_dict),
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    filtered_result = resp.json()
    assert set(filtered_result.keys()) == {"_/temperature", "_/safe"}

    # Verify temperature filter
    positive_temps = [
        d["_/temperature"]
        for d in data
        if "_/temperature" in d and d["_/temperature"] > 0
    ]
    if positive_temps:
        expected_temp_mean = sum(positive_temps) / len(positive_temps)
        assert abs(float(filtered_result["_/temperature"]) - expected_temp_mean) < 1e-6

    # Verify safe filter
    safe_vals = [1 for d in data if "_/safe" in d and d["_/safe"] is True]
    if safe_vals:
        expected_safe_mean = sum(safe_vals) / len(safe_vals)
        assert abs(float(filtered_result["_/safe"]) - expected_safe_mean) < 1e-6

    #
    # 6. Key-specific from_ids
    #
    from_ids_dict = {
        "_/temperature": "1&2",  # Only logs #1 and #2 for temperature
        "desc_len": "5&6",  # Only logs #5 and #6 for desc_len
    }
    resp = await client.get(
        f"/v0/logs/metric/mean?project_name={project_name}",
        params={
            "key": json.dumps(["_/temperature", "desc_len"]),
            "from_ids": json.dumps(from_ids_dict),
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    from_ids_result = resp.json()
    assert set(from_ids_result.keys()) == {"_/temperature", "desc_len"}

    #
    # 7. Key-specific exclude_ids
    #
    exclude_ids_dict = {
        "_/temperature": "3&4",  # Exclude logs 3 and 4 for temperature
        "_/safe": "2&3",  # Exclude logs 2 and 3 for safe
    }
    resp = await client.get(
        f"/v0/logs/metric/mean?project_name={project_name}",
        params={
            "key": json.dumps(["_/temperature", "_/safe"]),
            "exclude_ids": json.dumps(exclude_ids_dict),
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    exclude_ids_result = resp.json()
    assert set(exclude_ids_result.keys()) == {"_/temperature", "_/safe"}


@pytest.mark.anyio
async def test_get_logs_metric_grouped(client: AsyncClient):
    """Test the get_logs_metric endpoint with group_by parameter."""
    project_name = "test-metric-grouping"
    _ = await _create_project(client, project_name)

    # Create test data
    await _create_several_logs(client, project_name)

    # Create derived logs for testing
    # First derived log: temperature + 10
    derived_conf_temp = {
        "key": "derived_temp",
        "equation": "{t:_/temperature} + 10",
        "referenced_logs": {"t": [1, 2, 3, 4]},  # logs with temperature field
    }
    response = await _create_derived_entry(
        client,
        project_name,
        derived_conf_temp["key"],
        derived_conf_temp["equation"],
        derived_conf_temp["referenced_logs"],
    )
    assert response.status_code == 200

    # Second derived log: state length
    derived_conf_state = {
        "key": "state_len",
        "equation": "len({s:_/state})",
        "referenced_logs": {"s": [1, 2, 3, 4]},  # logs with state field
    }
    response = await _create_derived_entry(
        client,
        project_name,
        derived_conf_state["key"],
        derived_conf_state["equation"],
        derived_conf_state["referenced_logs"],
    )
    assert response.status_code == 200

    # Test 1: Simple metric without grouping (baseline)
    response = await client.get(
        f"/v0/logs/metric/mean?project_name={project_name}",
        params={"key": "_/temperature"},
        headers=HEADERS,
    )
    assert response.status_code == 200
    result = response.json()
    assert isinstance(
        result,
        (int, float, str),
    ), "Non-grouped result should be a scalar value"

    # Test 2: Single-level grouping by state
    response = await client.get(
        f"/v0/logs/metric/mean?project_name={project_name}",
        params={
            "key": "_/temperature",
            "group_by": "entries/_/state",
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    result = response.json()

    # Verify structure: should be a dictionary with state values as keys
    assert isinstance(result, dict), "Grouped result should be a dictionary"

    # Check that we have the expected state groups
    expected_states = ["liquid->gas", "liquid->solid", "gas"]
    for state in expected_states:
        assert state in result, f"Expected state '{state}' in grouped results"

    # Verify values for specific states
    # For liquid->gas state (boiling water), temperature should be 100.0
    assert np.isclose(result["liquid->gas"]["shared_value"], 100.0, atol=1e-6)

    # For liquid->solid state (freezing water and freezing nitrogen), mean should be (-210 + 0) / 2 = -105.0
    assert np.isclose(result["liquid->solid"]["mean"], -105.0, atol=1e-6)

    # Test 3: Single-level grouping by derived field
    response = await client.get(
        f"/v0/logs/metric/mean?project_name={project_name}",
        params={
            "key": "_/temperature",
            "group_by": "derived_entries/state_len",
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    result = response.json()

    assert isinstance(result, dict), "Grouped result should be a dictionary"
    # Check that we have groups based on state length
    # state_len for "liquid->gas" is 11, "liquid->solid" is 13, "gas" is 3
    assert "11.0" in result, "Expected state length '11' in grouped results"
    assert "13.0" in result, "Expected state length '13' in grouped results"
    assert "3.0" in result, "Expected state length '3' in grouped results"

    # Test 4: Multi-level grouping (nested)
    response = await client.get(
        f"/v0/logs/metric/mean?project_name={project_name}",
        params={
            "key": "_/temperature",
            "group_by": json.dumps(["entries/_/state", "entries/_/safe"]),
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    result = response.json()

    assert isinstance(result, dict), "Grouped result should be a dictionary"

    # First level should be state
    for state in expected_states:
        assert (
            state in result
        ), f"Expected state '{state}' in first level of nested grouping"
        assert isinstance(
            result[state],
            dict,
        ), f"Value for state '{state}' should be a dictionary"

        # Second level should be safe (true/false)
        safe_dict = result[state]
        if state == "liquid->solid":
            assert "True" in safe_dict, "Expected 'True' safety value for liquid->solid"
            assert (
                "False" in safe_dict
            ), "Expected 'False' safety value for liquid->solid"
            # freezing water (safe=true) has temp=0, freezing nitrogen (safe=false) has temp=-210
            assert np.isclose(safe_dict["True"]["shared_value"], 0.0, atol=1e-6)
            assert np.isclose(safe_dict["False"]["shared_value"], -210.0, atol=1e-6)
        elif state == "liquid->gas":
            assert "False" in safe_dict, "Expected 'False' safety value for liquid->gas"
            # boiling water (safe=false) has temp=100
            assert np.isclose(safe_dict["False"]["shared_value"], 100.0, atol=1e-6)
        elif state == "gas":
            assert "False" in safe_dict, "Expected 'False' safety value for gas"
            # surface of sun (safe=false) has temp=6000
            assert np.isclose(safe_dict["False"]["shared_value"], 6000.0, atol=1e-6)

    # Test 5: Grouping with filter expression
    response = await client.get(
        f"/v0/logs/metric/mean?project_name={project_name}",
        params={
            "key": "_/temperature",
            "group_by": "entries/_/state",
            "filter_expr": "_/safe is True",
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    result = response.json()

    assert isinstance(result, dict), "Grouped result should be a dictionary"
    # Only freezing water is safe, so only liquid->solid state with temp=0 should be present
    assert (
        "liquid->solid" in result
    ), "Expected 'liquid->solid' state in filtered results"
    assert np.isclose(result["liquid->solid"]["shared_value"], 0.0, atol=1e-6)
    assert (
        "liquid->gas" not in result
    ), "Unsafe 'liquid->gas' state should not be in filtered results"
    assert "gas" not in result, "Unsafe 'gas' state should not be in filtered results"

    # Test 6: Different metrics with grouping
    for metric in ["min", "max", "sum"]:
        response = await client.get(
            f"/v0/logs/metric/{metric}?project_name={project_name}",
            params={
                "key": "_/temperature",
                "group_by": "entries/_/state",
            },
            headers=HEADERS,
        )
        assert response.status_code == 200
        result = response.json()

        assert isinstance(
            result,
            dict,
        ), f"Grouped {metric} result should be a dictionary"

        # Check specific values for liquid->solid state
        if metric == "min":
            assert np.isclose(result["liquid->solid"][metric], -210.0, atol=1e-6)
        elif metric == "max":
            assert np.isclose(result["liquid->solid"][metric], 0.0, atol=1e-6)
        elif metric == "sum":
            assert np.isclose(result["liquid->solid"][metric], -210.0 + 0.0, atol=1e-6)


@pytest.mark.anyio
async def test_get_logs_metric_batched_with_grouping(
    client: AsyncClient,
):
    """Test the get_logs_metric endpoint with batched metrics and grouping."""
    project_name = "test-metric-batched-grouping"
    _ = await _create_project(client, project_name)

    # Create test data
    await _create_several_logs(client, project_name)

    # Create derived logs for testing
    # First derived log: temperature + 10
    derived_conf_temp = {
        "key": "derived_temp",
        "equation": "{t:_/temperature} + 10",
        "referenced_logs": {"t": [1, 2, 3, 4]},  # logs with temperature field
    }
    response = await _create_derived_entry(
        client,
        project_name,
        derived_conf_temp["key"],
        derived_conf_temp["equation"],
        derived_conf_temp["referenced_logs"],
    )
    assert response.status_code == 200

    # Second derived log: state length
    derived_conf_state = {
        "key": "state_len",
        "equation": "len({s:_/state})",
        "referenced_logs": {"s": [1, 2, 3, 4]},  # logs with state field
    }
    response = await _create_derived_entry(
        client,
        project_name,
        derived_conf_state["key"],
        derived_conf_state["equation"],
        derived_conf_state["referenced_logs"],
    )
    assert response.status_code == 200

    # Test 1: Batched metrics with single-level grouping
    response = await client.get(
        "/v0/logs/metric/mean",
        params={
            "project_name": project_name,
            "key": json.dumps(["_/temperature", "derived_temp", "state_len"]),
            "group_by": "entries/_/state",
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()

    # Verify structure: should be a dictionary with each key mapping to grouped results
    assert isinstance(result, dict), "Result should be a dictionary"
    assert "_/temperature" in result, "Expected '_/temperature' key in results"
    assert "derived_temp" in result, "Expected 'derived_temp' key in results"
    assert "state_len" in result, "Expected 'state_len' key in results"

    # Check that each key maps to a dictionary with state values as keys
    temp_results = result["_/temperature"]
    derived_temp_results = result["derived_temp"]
    state_len_results = result["state_len"]

    assert isinstance(temp_results, dict), "Temp results should be a dictionary"
    assert isinstance(
        derived_temp_results,
        dict,
    ), "Derived temp results should be a dictionary"
    assert isinstance(
        state_len_results,
        dict,
    ), "State length results should be a dictionary"

    # Check expected state groups in temperature results
    expected_states = ["liquid->gas", "liquid->solid", "gas"]
    for state in expected_states:
        assert state in temp_results, f"Expected state '{state}' in temp results"
        assert (
            state in derived_temp_results
        ), f"Expected state '{state}' in derived_temp results"

    # Verify specific values for each metric
    # For liquid->gas state (boiling water), temperature should be 100.0
    assert np.isclose(temp_results["liquid->gas"]["shared_value"], 100.0, atol=1e-6)
    # Derived temp should be 10 more than the original temperature
    assert np.isclose(
        derived_temp_results["liquid->gas"]["shared_value"],
        110.0,
        atol=1e-6,
    )

    # For liquid->solid state (freezing water and freezing nitrogen), mean should be (-210 + 0) / 2 = -105.0
    assert np.isclose(temp_results["liquid->solid"]["mean"], -105.0, atol=1e-6)
    # Derived temp should be 10 more than the original temperature
    assert np.isclose(derived_temp_results["liquid->solid"]["mean"], -95.0, atol=1e-6)

    # For gas state (surface of the sun), temperature should be 6000.0
    assert np.isclose(temp_results["gas"]["shared_value"], 6000.0, atol=1e-6)
    # Derived temp should be 10 more than the original temperature
    assert np.isclose(derived_temp_results["gas"]["shared_value"], 6010.0, atol=1e-6)

    # Check state_len values - state_len for "liquid->gas" is 11, "liquid->solid" is 13, "gas" is 3
    assert np.isclose(state_len_results["liquid->gas"]["shared_value"], 11.0, atol=1e-6)
    assert np.isclose(
        state_len_results["liquid->solid"]["shared_value"],
        13.0,
        atol=1e-6,
    )
    assert np.isclose(state_len_results["gas"]["shared_value"], 3.0, atol=1e-6)

    # Test 2: Batched metrics with multi-level (nested) grouping
    response = await client.get(
        "/v0/logs/metric/mean",
        params={
            "project_name": project_name,
            "key": json.dumps(["_/temperature", "derived_temp"]),
            "group_by": json.dumps(["entries/_/state", "entries/_/safe"]),
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()

    # Verify structure: should be a dictionary with each key mapping to grouped results
    assert isinstance(result, dict), "Result should be a dictionary"
    assert "_/temperature" in result, "Expected '_/temperature' key in results"
    assert "derived_temp" in result, "Expected 'derived_temp' key in results"

    # Check that each key maps to a dictionary with state values as keys
    temp_results = result["_/temperature"]
    derived_temp_results = result["derived_temp"]

    # First level should be state
    for state in expected_states:
        assert state in temp_results, f"Expected state '{state}' in temp results"
        assert (
            state in derived_temp_results
        ), f"Expected state '{state}' in derived_temp results"

        # Each state should map to a dictionary with safe values as keys
        temp_safe_dict = temp_results[state]
        derived_temp_safe_dict = derived_temp_results[state]

        assert isinstance(
            temp_safe_dict,
            dict,
        ), f"Expected dict for state '{state}' in temp results"
        assert isinstance(
            derived_temp_safe_dict,
            dict,
        ), f"Expected dict for state '{state}' in derived_temp results"

        # Check specific values for each state and safety combination
        if state == "liquid->solid":
            # Check both true and false safety values for liquid->solid
            assert (
                "True" in temp_safe_dict
            ), "Expected 'true' safety value for liquid->solid"
            assert (
                "False" in temp_safe_dict
            ), "Expected 'false' safety value for liquid->solid"

            # freezing water (safe=true) has temp=0, freezing nitrogen (safe=false) has temp=-210
            assert np.isclose(temp_safe_dict["True"]["shared_value"], 0.0, atol=1e-6)
            assert np.isclose(
                temp_safe_dict["False"]["shared_value"],
                -210.0,
                atol=1e-6,
            )

            # Derived temp should be 10 more than the original temperature
            assert np.isclose(
                derived_temp_safe_dict["True"]["shared_value"],
                10.0,
                atol=1e-6,
            )
            assert np.isclose(
                derived_temp_safe_dict["False"]["shared_value"],
                -200.0,
                atol=1e-6,
            )

        elif state == "liquid->gas":
            # Only false safety value for liquid->gas
            assert (
                "False" in temp_safe_dict
            ), "Expected 'false' safety value for liquid->gas"

            # boiling water (safe=false) has temp=100
            assert np.isclose(temp_safe_dict["False"]["shared_value"], 100.0, atol=1e-6)

            # Derived temp should be 10 more than the original temperature
            assert np.isclose(
                derived_temp_safe_dict["False"]["shared_value"],
                110.0,
                atol=1e-6,
            )

        elif state == "gas":
            # Only false safety value for gas
            assert "False" in temp_safe_dict, "Expected 'false' safety value for gas"

            # surface of sun (safe=false) has temp=6000
            assert np.isclose(
                temp_safe_dict["False"]["shared_value"],
                6000.0,
                atol=1e-6,
            )

            # Derived temp should be 10 more than the original temperature
            assert np.isclose(
                derived_temp_safe_dict["False"]["shared_value"],
                6010.0,
                atol=1e-6,
            )


@pytest.mark.anyio
async def test_get_logs_metric_shared_value_reduction(
    client: AsyncClient,
):
    """
    Test that the get_metrics endpoint correctly handles shared value reduction.

    When all values for a given key within a group are identical, the endpoint
    should return that shared value directly without performing any metric reduction.
    """
    project_name = "test-shared-value-reduction"
    _ = await _create_project(client, project_name)

    # Create logs with different groups
    # Group A: Three logs with the same 'score' value (10)
    group_a_logs = [
        {
            "group": "A",
            "score": 10,
            "text": "identical text for A",
            "is_valid": True,
            "config": {"mode": "test", "retry": 3},
        },
        {
            "group": "A",
            "score": 10,
            "text": "identical text for A",
            "is_valid": True,
            "config": {"mode": "test", "retry": 3},
        },
        {
            "group": "A",
            "score": 10,
            "text": "identical text for A",
            "is_valid": True,
            "config": {"mode": "test", "retry": 3},
        },
    ]

    # Group B: Three logs with different 'score' values that average to 10
    group_b_logs = [
        {
            "group": "B",
            "score": 5,
            "text": "identical text for B",
            "is_valid": True,
            "config": {"mode": "test", "retry": 3},
        },
        {
            "group": "B",
            "score": 10,
            "text": "identical text for B",
            "is_valid": True,
            "config": {"mode": "test", "retry": 3},
        },
        {
            "group": "B",
            "score": 15,
            "text": "identical text for B",
            "is_valid": True,
            "config": {"mode": "test", "retry": 3},
        },
    ]

    # Group C: Three logs with the same 'text' value
    group_c_logs = [
        {
            "group": "C",
            "score": 20,
            "text": "identical text",
            "is_valid": True,
            "config": {"mode": "test", "retry": 3},
        },
        {
            "group": "C",
            "score": 30,
            "text": "identical text",
            "is_valid": True,
            "config": {"mode": "test", "retry": 3},
        },
        {
            "group": "C",
            "score": 40,
            "text": "identical text",
            "is_valid": True,
            "config": {"mode": "test", "retry": 3},
        },
    ]

    # Group D: Three logs with different 'text' values
    group_d_logs = [
        {
            "group": "D",
            "score": 50,
            "text": "text 1",
            "is_valid": True,
            "config": {"mode": "test", "retry": 3},
        },
        {
            "group": "D",
            "score": 50,
            "text": "text 2",
            "is_valid": True,
            "config": {"mode": "test", "retry": 3},
        },
        {
            "group": "D",
            "score": 50,
            "text": "text 3",
            "is_valid": True,
            "config": {"mode": "test", "retry": 3},
        },
    ]

    # Group E: Three logs with identical boolean value for 'is_valid'
    group_e_logs = [
        {
            "group": "E",
            "score": 60,
            "text": "text for E1",
            "is_valid": True,
            "config": {"mode": "test", "retry": 3},
        },
        {
            "group": "E",
            "score": 70,
            "text": "text for E2",
            "is_valid": True,
            "config": {"mode": "test", "retry": 3},
        },
        {
            "group": "E",
            "score": 80,
            "text": "text for E3",
            "is_valid": True,
            "config": {"mode": "test", "retry": 3},
        },
    ]

    # Group F: Three logs with identical object value for 'config'
    group_f_logs = [
        {
            "group": "F",
            "score": 90,
            "text": "text for F1",
            "is_valid": True,
            "config": {"mode": "test", "retry": 3},
        },
        {
            "group": "F",
            "score": 100,
            "text": "text for F2",
            "is_valid": False,
            "config": {"mode": "test", "retry": 3},
        },
        {
            "group": "F",
            "score": 110,
            "text": "text for F3",
            "is_valid": True,
            "config": {"mode": "test", "retry": 3},
        },
    ]

    # Create all logs
    all_logs = (
        group_a_logs
        + group_b_logs
        + group_c_logs
        + group_d_logs
        + group_e_logs
        + group_f_logs
    )
    for entry in all_logs:
        response = await _create_log(client, project_name, entries=entry)
        assert response.status_code == 200, response.json()

    # Test 1: Numeric field with shared values (Group A) vs. different values (Group B)
    response = await client.get(
        f"/v0/logs/metric/mean?project_name={project_name}",
        params={
            "key": "score",
            "group_by": "entries/group",
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    result = response.json()

    # Verify structure: should be a dictionary with group values as keys
    assert isinstance(result, dict), "Grouped result should be a dictionary"

    # For Group A, all scores are 10, so the result should be exactly 10 (shared value)
    assert (
        "shared_value" in result["A"]
    ), "Group A should return the shared value 10 directly"
    assert (
        result["A"]["shared_value"] == 10
    ), "Group A should return the shared value 10 directly"

    # For Group B, scores are 5, 10, 15, so the mean is 10
    assert "mean" in result["B"], "Group B should return the mean value"
    assert np.isclose(
        result["B"]["mean"],
        10.0,
        atol=1e-6,
    ), "Group B should compute the mean as 10.0"

    # Test 2: String field with shared values (Group C) vs. different values (Group D)
    response = await client.get(
        f"/v0/logs/metric/mean?project_name={project_name}",
        params={
            "key": "text",
            "group_by": "entries/group",
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    result = response.json()

    # For Group C, all texts are "identical text", so the result should be that exact string
    assert (
        "shared_value" in result["C"]
    ), "Group C should return the shared text value directly"
    assert (
        result["C"]["shared_value"] == "identical text"
    ), "Group C should return the shared text value directly"

    # For Group D, texts are different, so the result should be numeric
    assert "mean" in result["D"], "Group D should return the mean value"
    assert np.isclose(
        result["D"]["mean"],
        6.0,
        atol=1e-6,
    ), "Group D should compute the mean as 6.0"

    # Test 3: Boolean field with shared values (Group E)
    response = await client.get(
        f"/v0/logs/metric/mean?project_name={project_name}",
        params={
            "key": "is_valid",
            "group_by": "entries/group",
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    result = response.json()

    # For Group E, all is_valid values are True, so the result should be True
    assert (
        "shared_value" in result["E"]
    ), "Group E should return the shared boolean value True directly"
    assert (
        result["E"]["shared_value"] is True
    ), "Group E should return the shared boolean value True directly"

    # For Group F, is_valid values are mixed (True, False, True), so no shared value
    assert "mean" in result["F"], "Group F should return the mean value"
    assert (
        result["F"] is not True
    ), "Group F should not return True for mixed boolean values"

    # Test 4: Object field with shared values (Group F)
    response = await client.get(
        f"/v0/logs/metric/mean?project_name={project_name}",
        params={
            "key": "config",
            "group_by": "entries/group",
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    result = response.json()

    # For Group F, all config objects are identical, so the result should be that object
    expected_config = {"mode": "test", "retry": 3}
    assert (
        "shared_value" in result["F"]
    ), "Group F should return the shared config object directly"
    assert (
        result["F"]["shared_value"] == expected_config
    ), "Group F should return the shared config object directly"

    # Test 5: Verify shared value reduction for idempotent metrics on Group A
    # Group A has 3 logs with score=10 — shared_value is only valid for
    # metrics where f([V,V,...,V]) == V (min, max, median).
    for metric in ["min", "max", "median"]:
        response = await client.get(
            f"/v0/logs/metric/{metric}?project_name={project_name}",
            params={
                "key": "score",
                "group_by": "entries/group",
            },
            headers=HEADERS,
        )
        assert response.status_code == 200
        result = response.json()
        assert (
            result["A"]["shared_value"] == 10
        ), f"Group A should return the shared value 10 directly for metric {metric}"

    # Test 5b: sum must use the SQL aggregate, not shared_value
    # 3 identical scores of 10 → sum = 30
    response = await client.get(
        f"/v0/logs/metric/sum?project_name={project_name}",
        params={"key": "score", "group_by": "entries/group"},
        headers=HEADERS,
    )
    assert response.status_code == 200
    result = response.json()
    assert result["A"]["shared_value"] is None, "sum must not use shared_value"
    assert result["A"]["sum"] == 30, "sum of 3 × 10 should be 30"

    # Test 5c: count must use the SQL aggregate
    # 3 logs in Group A → count = 3
    response = await client.get(
        f"/v0/logs/metric/count?project_name={project_name}",
        params={"key": "score", "group_by": "entries/group"},
        headers=HEADERS,
    )
    assert response.status_code == 200
    result = response.json()
    assert result["A"]["shared_value"] is None, "count must not use shared_value"
    assert result["A"]["count"] == 3, "count of 3 identical values should be 3"

    # Test 5d: var/std of identical values should be 0
    for metric in ["var", "std"]:
        response = await client.get(
            f"/v0/logs/metric/{metric}?project_name={project_name}",
            params={"key": "score", "group_by": "entries/group"},
            headers=HEADERS,
        )
        assert response.status_code == 200
        result = response.json()
        assert (
            result["A"]["shared_value"] is None
        ), f"{metric} must not use shared_value"
        assert float(result["A"][metric]) == pytest.approx(
            0.0,
        ), f"{metric} of identical values should be 0"


@pytest.mark.anyio
async def test_get_logs_metric_time_date_timedelta(client: AsyncClient):
    """
    Test the get_logs_metric endpoint with time, date, and timedelta data types.

    This test creates logs with time, date, and timedelta fields and verifies that
    the endpoint correctly processes and formats these special data types.
    """
    project_name = "test-time-date-timedelta"
    _ = await _create_project(client, project_name)

    # Create logs with time, date, and timedelta values
    time_logs = [
        {
            "time_value": "08:30:00",  # Morning time
            "group": "A",
        },
        {
            "time_value": "12:00:00",  # Noon
            "group": "A",
        },
        {
            "time_value": "17:45:30",  # Evening time
            "group": "B",
        },
        {
            "time_value": "23:59:59",  # Late night
            "group": "B",
        },
    ]

    date_logs = [
        {
            "date_value": "2025-01-15",  # January
            "group": "A",
        },
        {
            "date_value": "2025-02-28",  # February
            "group": "A",
        },
        {
            "date_value": "2025-03-15",  # March
            "group": "B",
        },
        {
            "date_value": "2025-12-31",  # December
            "group": "B",
        },
    ]

    timedelta_logs = [
        {
            "timedelta_value": "P1DT6H",  # 1 day, 6 hours
            "group": "A",
        },
        {
            "timedelta_value": "P2DT12H",  # 2 days, 12 hours
            "group": "A",
        },
        {
            "timedelta_value": "PT12H30M",  # 12 hours, 30 minutes
            "group": "B",
        },
        {
            "timedelta_value": "P5DT8H15M",  # 5 days, 8 hours, 15 minutes
            "group": "B",
        },
    ]

    # Create all logs
    for entry in time_logs + date_logs + timedelta_logs:
        entry = {
            **entry,
            "explicit_types": {
                "time_value": {
                    "type": "time",
                },
                "date_value": {
                    "type": "date",
                },
                "timedelta_value": {
                    "type": "timedelta",
                },
            },
        }
        response = await _create_log(client, project_name, entries=entry)
        assert response.status_code == 200, response.json()

    # Test time values with different metrics
    for metric in ["mean", "min", "max", "var", "std"]:  # "sum",
        response = await client.get(
            f"/v0/logs/metric/{metric}?project_name={project_name}",
            params={"key": "time_value"},
            headers=HEADERS,
        )
        assert response.status_code == 200, response.json()
        result = response.json()

        # For time values, result should contain colons (HH:MM:SS format)
        if metric in ["mean", "min", "max", "sum"]:
            assert ":" in result, f"Time result for {metric} should contain colons"

        # For statistical metrics, the result might be a numeric value
        # but we still expect some formatting to indicate it's a time
        if metric in ["var", "std"]:
            assert isinstance(
                result,
                (int, float, str),
            ), f"Expected numeric or string result for {metric}"
            if isinstance(result, str):
                assert (
                    ":" in result or "seconds" in result.lower()
                ), f"Time variance/std result should contain colons or mention seconds"

    # Test date values with different metrics
    for metric in ["mean", "min", "max", "sum", "var", "std"]:
        response = await client.get(
            f"/v0/logs/metric/{metric}?project_name={project_name}",
            params={"key": "date_value"},
            headers=HEADERS,
        )
        assert response.status_code == 200, response.json()
        result = response.json()

        # For date values, result should contain hyphens (YYYY-MM-DD format)
        if metric in ["mean", "min", "max", "sum"]:
            assert "-" in result, f"Date result for {metric} should contain hyphens"

        # For statistical metrics, the result might be a numeric value
        # but we still expect some formatting to indicate it's a date
        if metric in ["var", "std"]:
            assert isinstance(
                result,
                (int, float, str),
            ), f"Expected numeric or string result for {metric}"
            if isinstance(result, str):
                assert (
                    "-" in result
                    or "days" in result.lower()
                    or "seconds" in result.lower()
                ), f"Date variance/std result should contain hyphens or mention days"

    # Test timedelta values with different metrics
    for metric in ["mean", "min", "max", "sum", "var", "std"]:
        response = await client.get(
            f"/v0/logs/metric/{metric}?project_name={project_name}",
            params={"key": "timedelta_value"},
            headers=HEADERS,
        )
        assert response.status_code == 200, response.json()
        result = response.json()

        # For timedelta values, result should follow ISO 8601 duration format
        if metric in ["mean", "min", "max", "sum"]:
            assert (
                "P" in result
            ), f"Timedelta result for {metric} should contain 'P' (ISO 8601 format)"
            assert (
                "T" in result
            ), f"Timedelta result for {metric} should contain 'T' (ISO 8601 format)"

        # For statistical metrics, the result might be a numeric value
        # but we still expect some formatting to indicate it's a duration
        if metric in ["var", "std"]:
            assert isinstance(
                result,
                (int, float, str),
            ), f"Expected numeric or string result for {metric}"
            if isinstance(result, str):
                assert (
                    "P" in result
                    or "days" in result.lower()
                    or "seconds" in result.lower()
                ), f"Timedelta variance/std result should follow ISO format or mention time units"


@pytest.mark.anyio
async def test_get_logs_metric_with_mixed_null_float_derived_column(
    client: AsyncClient,
):
    """
    Test the get_logs_metric endpoint with a derived column that has both null and float values.

    This test creates:
    1. A derived column based on existing columns that results in mixed null/float values
    2. Calls the metrics endpoint with all field names including the derived column
    3. Ensures no exceptions are thrown and the call succeeds
    """
    project_name = "test-mixed-null-float-derived"
    await _create_project(client, project_name)

    # Create logs with mixed temperature values - some null, some valid
    log_entries = [
        # Log 1: Valid temperature
        {"temperature": 25.0, "location": "indoor", "humidity": 60},
        # Log 2: Null temperature
        {"temperature": None, "location": "outdoor", "humidity": 45},
        # Log 3: Missing temperature field entirely
        {"location": "basement", "humidity": 70},
        # Log 4: Another valid temperature
        {"temperature": 30.0, "location": "attic", "humidity": 55},
        # Log 5: Zero temperature (valid)
        {"temperature": 0.0, "location": "freezer", "humidity": 80},
        # Log 6: Null temperature again
        {"temperature": None, "location": "garage", "humidity": 50},
    ]

    # Create all the logs
    log_ids = []
    for entry in log_entries:
        response = await _create_log(client, project_name, entries=entry)
        assert response.status_code == 200
        log_ids.append(response.json()["log_event_ids"][0])

    # Create a derived column that will have mixed null/float values
    # This equation will result in float values for valid temperatures and null for null/missing temperatures
    derived_key = "temp_fahrenheit"
    equation = "{log:temperature} * 9 / 5 + 32"  # Celsius to Fahrenheit conversion
    referenced_logs = {"log": log_ids}

    response = await _create_derived_entry(
        client,
        project_name,
        derived_key,
        equation,
        referenced_logs,
    )
    assert response.status_code == 200

    # Verify the derived column has mixed null/float values as expected
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    logs = response.json()["logs"]

    # Get all field names from the logs
    all_field_names = set()
    for log in logs:
        # Add base entry field names
        all_field_names.update(log["entries"].keys())
        # Add derived entry field names
        all_field_names.update(log["derived_entries"].keys())

    # Convert to list for the API call
    all_fields_list = list(all_field_names)

    # Test the metrics endpoint with all field names including the derived column
    # This should not throw an exception even though the derived column has mixed null/float values
    try:
        response = await client.get(
            f"/v0/logs/metric/mean?project_name={project_name}",
            params={"key": json.dumps(all_fields_list)},
            headers=HEADERS,
        )

        # If we get here, no exception was thrown - this is what we want
        assert (
            response.status_code == 200
        ), f"Expected 200 status code, got {response.status_code}: {response.text}"

        result = response.json()
        assert isinstance(result, dict), "Expected dictionary result for multiple keys"

        # Verify that all requested fields are in the result
        assert set(result.keys()) == set(
            all_fields_list,
        ), f"Expected keys {all_fields_list}, got {result.keys()}"

        # Verify the derived column has a float result (not null)
        assert (
            derived_key in result
        ), f"Derived key '{derived_key}' should be in results"
        derived_result = result[derived_key]
        assert isinstance(
            derived_result,
            (int, float),
        ), f"Derived column result should be numeric, got {type(derived_result)}: {derived_result}"

        # NULL values in derived columns are excluded from mean
        # mean of [77.0, 86.0, 32.0] = 65.0
        expected_mean = (77.0 + 86.0 + 32.0) / 3
        assert (
            abs(derived_result - expected_mean) < 0.001
        ), f"Expected derived mean ~{expected_mean}, got {derived_result}"

        # Test passed - no exception was thrown

    except Exception as e:
        # If any exception is thrown, the test should fail
        assert (
            False
        ), f"Metrics endpoint threw an exception when it shouldn't have: {type(e).__name__}: {str(e)}"

    # Also test with individual metrics to ensure robustness
    for metric in ["min", "max", "sum", "count"]:
        try:
            response = await client.get(
                f"/v0/logs/metric/{metric}?project_name={project_name}",
                params={"key": json.dumps(all_fields_list)},
                headers=HEADERS,
            )
            assert (
                response.status_code == 200
            ), f"Expected 200 for {metric}, got {response.status_code}"

            result = response.json()
            assert isinstance(result, dict), f"Expected dictionary result for {metric}"
            assert derived_key in result, f"Derived key should be in {metric} results"

        except Exception as e:
            assert (
                False
            ), f"Metrics endpoint threw an exception for {metric}: {type(e).__name__}: {str(e)}"


@pytest.mark.anyio
async def test_get_logs_metric_grouped_with_mixed_null_float_derived_column(
    client: AsyncClient,
):
    """
    Test the get_logs_metric endpoint with grouping and batched keys including a derived column
    that has both null and float values. This specifically tests _compute_metric_for_key_grouped.

    This test creates:
    1. A derived column based on existing columns that results in mixed null/float values
    2. Calls the metrics endpoint with group_by and multiple keys including the derived column
    3. Ensures no exceptions are thrown and the call succeeds
    """
    project_name = "test-grouped-mixed-null-float-derived"
    await _create_project(client, project_name)

    # Create logs with mixed temperature values and grouping categories
    log_entries = [
        # Group A logs with mixed temperature values
        {"temperature": 25.0, "location": "indoor", "humidity": 60, "category": "A"},
        {"temperature": None, "location": "outdoor", "humidity": 45, "category": "A"},
        {
            "location": "basement",
            "humidity": 70,
            "category": "A",
        },  # Missing temperature
        # Group B logs with mixed temperature values
        {"temperature": 30.0, "location": "attic", "humidity": 55, "category": "B"},
        {"temperature": 0.0, "location": "freezer", "humidity": 80, "category": "B"},
        {"temperature": None, "location": "garage", "humidity": 50, "category": "B"},
        # Group C logs with mixed temperature values
        {"temperature": 35.0, "location": "office", "humidity": 65, "category": "C"},
        {"location": "storage", "humidity": 40, "category": "C"},  # Missing temperature
    ]

    # Create all the logs
    log_ids = []
    for entry in log_entries:
        response = await _create_log(client, project_name, entries=entry)
        assert response.status_code == 200
        log_ids.append(response.json()["log_event_ids"][0])

    # Create a derived column that will have mixed null/float values
    derived_key = "temp_fahrenheit"
    equation = "{log:temperature} * 9 / 5 + 32"  # Celsius to Fahrenheit conversion
    referenced_logs = {"log": log_ids}

    response = await _create_derived_entry(
        client,
        project_name,
        derived_key,
        equation,
        referenced_logs,
    )
    assert response.status_code == 200

    # Verify the derived column has mixed null/float values as expected
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    logs = response.json()["logs"]

    # Get all field names from the logs
    all_field_names = set()
    for log in logs:
        # Add base entry field names
        all_field_names.update(log["entries"].keys())
        # Add derived entry field names
        all_field_names.update(log["derived_entries"].keys())

    # Convert to list for the API call
    all_fields_list = list(all_field_names)

    # Test the metrics endpoint with grouping and multiple keys including the derived column
    # This should trigger _compute_metric_for_key_grouped and not throw an exception
    try:
        response = await client.get(
            f"/v0/logs/metric/mean?project_name={project_name}",
            params={
                "key": json.dumps(all_fields_list),
                "group_by": "Entries/category",
            },
            headers=HEADERS,
        )

        # If we get here, no exception was thrown - this is what we want
        assert (
            response.status_code == 200
        ), f"Expected 200 status code, got {response.status_code}: {response.text}"

        result = response.json()
        assert isinstance(
            result,
            dict,
        ), "Expected dictionary result for grouped metrics"

        # Verify that all keys are in the grouped results
        for key in all_fields_list:
            assert key in result, f"Key '{key}' should be in grouped results"

            # Each key should map to a dictionary with category groups
            key_results = result[key]
            assert isinstance(
                key_results,
                dict,
            ), f"Results for key '{key}' should be a dictionary"

        # Verify the derived column results
        if derived_key in result:
            derived_results = result[derived_key]
            for group, group_result in derived_results.items():
                if isinstance(group_result, dict) and "mean" in group_result:
                    # This group has multiple values, check the mean
                    # Mean can be None if all values in the group are None
                    # or numeric if at least some values are numeric
                    mean_value = group_result["mean"]
                    assert mean_value is None or isinstance(
                        mean_value,
                        (int, float),
                    ), f"Derived column mean for group {group} should be numeric or None, got {type(mean_value)}"
                elif isinstance(group_result, dict) and "shared_value" in group_result:
                    # This group has a single shared value
                    # Shared value can be None if the value is None
                    shared_value = group_result["shared_value"]
                    assert shared_value is None or isinstance(
                        shared_value,
                        (int, float),
                    ), f"Derived column shared_value for group {group} should be numeric or None, got {type(shared_value)}"

        # Test passed - no exception was thrown

    except Exception as e:
        # If any exception is thrown, the test should fail
        assert (
            False
        ), f"Grouped metrics endpoint threw an exception when it shouldn't have: {type(e).__name__}: {str(e)}"


@pytest.mark.anyio
async def test_get_logs_metric_key_specific_filters_with_mixed_null_float_derived_column(
    client: AsyncClient,
):
    """
    Test the get_logs_metric endpoint with key-specific filter expressions and batched keys
    including a derived column that has both null and float values. This specifically tests
    compute_metric_for_key.

    This test creates:
    1. A derived column based on existing columns that results in mixed null/float values
    2. Calls the metrics endpoint with key-specific filter expressions and multiple keys
    3. Ensures no exceptions are thrown and the call succeeds
    """
    project_name = "test-key-filters-mixed-null-float-derived"
    await _create_project(client, project_name)

    # Create logs with mixed temperature values and varying humidity levels
    log_entries = [
        # High humidity logs with mixed temperature values
        {"temperature": 25.0, "location": "indoor", "humidity": 80, "active": True},
        {"temperature": None, "location": "outdoor", "humidity": 85, "active": True},
        {
            "location": "basement",
            "humidity": 90,
            "active": False,
        },  # Missing temperature
        # Medium humidity logs with mixed temperature values
        {"temperature": 30.0, "location": "attic", "humidity": 60, "active": True},
        {"temperature": 0.0, "location": "freezer", "humidity": 55, "active": False},
        {"temperature": None, "location": "garage", "humidity": 65, "active": True},
        # Low humidity logs with mixed temperature values
        {"temperature": 35.0, "location": "office", "humidity": 40, "active": True},
        {"location": "storage", "humidity": 35, "active": False},  # Missing temperature
        {"temperature": 20.0, "location": "bedroom", "humidity": 45, "active": True},
    ]

    # Create all the logs
    log_ids = []
    for entry in log_entries:
        response = await _create_log(client, project_name, entries=entry)
        assert response.status_code == 200
        log_ids.append(response.json()["log_event_ids"][0])

    # Create a derived column that will have mixed null/float values
    derived_key = "temp_fahrenheit"
    equation = "{log:temperature} * 9 / 5 + 32"  # Celsius to Fahrenheit conversion
    referenced_logs = {"log": log_ids}

    response = await _create_derived_entry(
        client,
        project_name,
        derived_key,
        equation,
        referenced_logs,
    )
    assert response.status_code == 200

    # Verify the derived column has mixed null/float values as expected
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    logs = response.json()["logs"]

    # Get all field names from the logs
    all_field_names = set()
    for log in logs:
        # Add base entry field names
        all_field_names.update(log["entries"].keys())
        # Add derived entry field names
        all_field_names.update(log["derived_entries"].keys())

    # Convert to list for the API call
    all_fields_list = list(all_field_names)

    # Create key-specific filter expressions
    # This will force the use of compute_metric_for_key instead of bulk computation
    filter_expr_dict = {
        "humidity": "humidity > 50",  # Only high humidity logs
        "temperature": "temperature is not None",  # Only logs with temperature
        derived_key: f"{derived_key} is not None",  # Only logs with derived values
        "active": "active is True",  # Only active logs
    }

    # Test the metrics endpoint with key-specific filters and multiple keys
    # This should trigger compute_metric_for_key and not throw an exception
    try:
        response = await client.get(
            f"/v0/logs/metric/mean?project_name={project_name}",
            params={
                "key": json.dumps(all_fields_list),
                "filter_expr": json.dumps(filter_expr_dict),
            },
            headers=HEADERS,
        )

        # If we get here, no exception was thrown - this is what we want
        assert (
            response.status_code == 200
        ), f"Expected 200 status code, got {response.status_code}: {response.text}"

        result = response.json()
        assert isinstance(result, dict), "Expected dictionary result for multiple keys"

        # Verify that all requested fields are in the result
        assert set(result.keys()) == set(
            all_fields_list,
        ), f"Expected keys {all_fields_list}, got {result.keys()}"

        # Verify the derived column has a float result (not null)
        assert (
            derived_key in result
        ), f"Derived key '{derived_key}' should be in results"
        derived_result = result[derived_key]
        assert isinstance(
            derived_result,
            (int, float),
        ), f"Derived column result should be numeric, got {type(derived_result)}: {derived_result}"

        # Verify other fields have appropriate results
        for key, value in result.items():
            if key in filter_expr_dict:
                # Fields with specific filters should have non-null results
                assert (
                    value is not None
                ), f"Field '{key}' with filter should have non-null result"

        # Test passed - no exception was thrown

    except Exception as e:
        # If any exception is thrown, the test should fail
        assert (
            False
        ), f"Key-specific filter metrics endpoint threw an exception when it shouldn't have: {type(e).__name__}: {str(e)}"

    # Also test with individual metrics to ensure robustness
    for metric in ["min", "max", "sum", "count"]:
        try:
            response = await client.get(
                f"/v0/logs/metric/{metric}?project_name={project_name}",
                params={
                    "key": json.dumps(all_fields_list),
                    "filter_expr": json.dumps(filter_expr_dict),
                },
                headers=HEADERS,
            )
            assert (
                response.status_code == 200
            ), f"Expected 200 for {metric}, got {response.status_code}"

            result = response.json()
            assert isinstance(result, dict), f"Expected dictionary result for {metric}"
            assert derived_key in result, f"Derived key should be in {metric} results"

        except Exception as e:
            assert (
                False
            ), f"Key-specific filter metrics endpoint threw an exception for {metric}: {type(e).__name__}: {str(e)}"


@pytest.mark.anyio
async def test_metrics_count_matches_rows_and_resets_on_context_delete(
    client: AsyncClient,
):
    """
    Fundamental reproduction for metric count inconsistency on auto-increment fields using Orchestra endpoints.

    Steps:
      1) Create a fresh context with an auto-incrementing "row_id" field.
      2) Verify the metric count starts at 0 (fresh context).
      3) Insert three rows without specifying "row_id" (auto-counting assigns ids).
      4) Verify get_logs(...) returns 3 rows and metric count == 3.
      5) Delete the context and recreate it.
      6) Verify metric count resets to 0 for the recreated context.
    """
    project_name = f"metrics-count-{uuid.uuid4().hex[:8]}"
    ctx = f"tests/local_storage/metrics/{uuid.uuid4().hex}"

    def _as_int0(v):
        return 0 if v is None else int(v)

    async def _create_ctx():
        # Ensure a context with auto-counting on 'row_id'
        resp = await client.post(
            f"/v0/project/{project_name}/contexts",
            json={
                "name": ctx,
                "unique_keys": {"row_id": "int"},
                "auto_counting": {"row_id": None},
                "description": "Metrics test context",
            },
            headers=HEADERS,
        )
        assert resp.status_code == 200, resp.text
        # Create fields explicitly for clarity
        resp = await client.post(
            "/v0/logs/fields",
            json={
                "project_name": project_name,
                "context": ctx,
                "fields": {
                    "row_id": {"type": "int"},
                    "name": {"type": "str"},
                },
            },
            headers=HEADERS,
        )
        assert resp.status_code == 200, resp.text

    async def _seed_filler_contexts(num: int = 10):
        for i in range(num):
            filler = f"{ctx}-filler-{i}-{uuid.uuid4().hex[:6]}"
            resp = await client.post(
                f"/v0/project/{project_name}/contexts",
                json={
                    "name": filler,
                    "description": "filler",
                    "unique_keys": {"row_id": "int"},
                    "auto_counting": {"row_id": None},
                },
                headers=HEADERS,
            )
            assert resp.status_code == 200, resp.text
            for j in range(i + 1):
                r = await _create_log(
                    client,
                    project_name,
                    entries={"name": f"F{i}-{j}"},
                    context=filler,
                )
                assert r.status_code == 200, r.text

    # Create project
    _ = await _create_project(client, project_name)

    try:
        # Create fresh context
        await _create_ctx()

        # Seed filler contexts under same project
        await _seed_filler_contexts()

        # 2) Initial metric should be zero in a fresh context
        resp = await client.get(
            "/v0/logs/metric/count",
            params={
                "project_name": project_name,
                "key": "row_id",
                "context": ctx,
            },
            headers=HEADERS,
        )
        assert resp.status_code == 200, resp.text
        initial_metric = resp.json()
        assert _as_int0(initial_metric) == 0

        # 3) Insert three rows; 'row_id' is auto-incremented by the backend
        for name in ["A", "B", "C"]:
            r = await _create_log(
                client,
                project_name,
                entries={"name": name},
                context=ctx,
            )
            assert r.status_code == 200, r.text

        # 4) Verify get_logs(...) returns 3 rows and metric count == 3
        rows_resp = await client.get(
            "/v0/logs",
            params={"project_name": project_name, "context": ctx},
            headers=HEADERS,
        )
        assert rows_resp.status_code == 200, rows_resp.text
        row_count = len(rows_resp.json()["logs"])

        metric_resp = await client.get(
            "/v0/logs/metric/count",
            params={
                "project_name": project_name,
                "key": "row_id",
                "context": ctx,
            },
            headers=HEADERS,
        )
        assert metric_resp.status_code == 200, metric_resp.text
        metric_after = metric_resp.json()

        assert row_count == 3, f"Expected 3 rows, got {row_count} (context={ctx})"
        assert (
            _as_int0(metric_after) == row_count
        ), f"Metric/row mismatch in context={ctx}: metric={_as_int0(metric_after)}, rows={row_count}"

        # 5) Delete the context and recreate; metric must reset to 0
        del_resp = await client.delete(
            f"/v0/project/{project_name}/contexts/{ctx}",
            headers=HEADERS,
        )
        assert del_resp.status_code == 200, del_resp.text

        await _create_ctx()

        metric_reset_resp = await client.get(
            "/v0/logs/metric/count",
            params={
                "project_name": project_name,
                "key": "row_id",
                "context": ctx,
            },
            headers=HEADERS,
        )
        assert metric_reset_resp.status_code == 200, metric_reset_resp.text
        metric_reset = metric_reset_resp.json()
        assert _as_int0(metric_reset) == 0

    finally:
        # Best-effort cleanup
        try:
            await client.delete(
                f"/v0/project/{project_name}/contexts/{ctx}",
                headers=HEADERS,
            )
        except Exception:
            pass


@pytest.mark.anyio
async def test_metrics_max_matches_row_ids_and_resets_on_context_delete(
    client: AsyncClient,
):
    """
    Fundamental check for the "max" metric on an auto-increment field using Orchestra endpoints.

    Steps:
      1) Create a fresh context with auto-incrementing "row_id".
      2) Verify initial max metric is 0 (empty context).
      3) Insert three rows; read back the rows and compute max(row_id) from entries.
      4) Verify get_logs_metric("max", key="row_id") equals that computed max.
      5) Delete the context and recreate; verify max resets to 0.
    """
    project_name = f"metrics-max-{uuid.uuid4().hex[:8]}"
    ctx = f"tests/local_storage/metrics_max/{uuid.uuid4().hex}"

    def _as_int0(v):
        return 0 if v is None else int(v)

    async def _create_ctx():
        resp = await client.post(
            f"/v0/project/{project_name}/contexts",
            json={
                "name": ctx,
                "unique_keys": {"row_id": "int"},
                "auto_counting": {"row_id": None},
                "description": "Metrics test context (max)",
            },
            headers=HEADERS,
        )
        assert resp.status_code == 200, resp.text
        resp = await client.post(
            "/v0/logs/fields",
            json={
                "project_name": project_name,
                "context": ctx,
                "fields": {
                    "row_id": {"type": "int"},
                    "name": {"type": "str"},
                },
            },
            headers=HEADERS,
        )
        assert resp.status_code == 200, resp.text

    async def _seed_filler_contexts(num: int = 10):
        for i in range(num):
            filler = f"{ctx}-filler-{i}-{uuid.uuid4().hex[:6]}"
            resp = await client.post(
                f"/v0/project/{project_name}/contexts",
                json={
                    "name": filler,
                    "description": "filler",
                    "unique_keys": {"row_id": "int"},
                    "auto_counting": {"row_id": None},
                },
                headers=HEADERS,
            )
            assert resp.status_code == 200, resp.text
            for j in range(i + 1):
                r = await _create_log(
                    client,
                    project_name,
                    entries={"name": f"F{i}-{j}"},
                    context=filler,
                )
                assert r.status_code == 200, r.text

    _ = await _create_project(client, project_name)

    try:
        await _create_ctx()

        # Seed filler contexts under same project
        await _seed_filler_contexts()

        # 2) Initial max metric should be zero in a fresh context
        resp = await client.get(
            "/v0/logs/metric/max",
            params={
                "project_name": project_name,
                "key": "row_id",
                "context": ctx,
            },
            headers=HEADERS,
        )
        assert resp.status_code == 200, resp.text
        initial_max = resp.json()
        assert _as_int0(initial_max) == 0

        # 3) Insert three rows; backend assigns row_id automatically
        created_row_ids: List[int] = []
        for name in ["A", "B", "C"]:
            r = await _create_log(
                client,
                project_name,
                entries={"name": name},
                context=ctx,
            )
            assert r.status_code == 200, r.text
            j = r.json()
            try:
                ids = j.get("row_ids", {}).get("ids", [])
                if ids and isinstance(ids, list) and ids[0]:
                    # Take the first id in case multiple unique cols
                    created_row_ids.append(int(ids[0][0]))
            except Exception:
                pass

        # Read back the rows and compute max(row_id)
        rows_resp = await client.get(
            "/v0/logs",
            params={"project_name": project_name, "context": ctx},
            headers=HEADERS,
        )
        assert rows_resp.status_code == 200, rows_resp.text
        logs = rows_resp.json()["logs"]

        row_ids: List[int] = []
        for lg in logs:
            entries = lg.get("entries", {}) if isinstance(lg, dict) else {}
            rid = entries.get("row_id")
            if isinstance(rid, int):
                row_ids.append(rid)

        # Fallback to creation responses if row_id isn't materialized in entries
        if len(row_ids) < 3 and created_row_ids:
            row_ids = created_row_ids

        assert (
            len(row_ids) == 3
        ), f"Expected 3 row_id values after inserts, got {len(row_ids)} (context={ctx})"
        computed_max = max(row_ids) if row_ids else 0

        metric_resp = await client.get(
            "/v0/logs/metric/max",
            params={
                "project_name": project_name,
                "key": "row_id",
                "context": ctx,
            },
            headers=HEADERS,
        )
        assert metric_resp.status_code == 200, metric_resp.text
        metric_max_after = metric_resp.json()
        assert _as_int0(metric_max_after) == computed_max, (
            f"Metric max mismatch in context={ctx}: metric={_as_int0(metric_max_after)}, "
            f"rows_max={computed_max}, rows={sorted(row_ids)}"
        )

        # 5) Delete the context and recreate; metric must reset to 0
        del_resp = await client.delete(
            f"/v0/project/{project_name}/contexts/{ctx}",
            headers=HEADERS,
        )
        assert del_resp.status_code == 200, del_resp.text

        await _create_ctx()

        metric_reset_resp = await client.get(
            "/v0/logs/metric/max",
            params={
                "project_name": project_name,
                "key": "row_id",
                "context": ctx,
            },
            headers=HEADERS,
        )
        assert metric_reset_resp.status_code == 200, metric_reset_resp.text
        metric_max_reset = metric_reset_resp.json()
        assert _as_int0(metric_max_reset) == 0

    finally:
        await client.delete(
            f"/v0/project/{project_name}/contexts/{ctx}",
            headers=HEADERS,
        )
