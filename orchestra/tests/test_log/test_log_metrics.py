import json
from typing import List, Optional

import numpy as np
import pytest
from httpx import AsyncClient

from ...web.api.log.helpers import _is_all_unique, reduction_methods
from . import (
    HEADERS,
    _create_derived_entry,
    _create_log,
    _create_project,
    _create_several_logs,
    log_data,
)


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
        f"/v0/logs/metric/{metric}?project={project_name}",
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
        f"/v0/logs/metric/mean?project={project_name}&key={single_key}",
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
        f"/v0/logs/metric/mean?project={project_name}",
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
        f"/v0/logs/metric/mean?project={project_name}",
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
        f"/v0/logs/metric/mean?project={project_name}",
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
        f"/v0/logs/metric/mean?project={project_name}",
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
        f"/v0/logs/metric/mean?project={project_name}",
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
        f"/v0/logs/metric/mean?project={project_name}",
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
        f"/v0/logs/metric/mean?project={project_name}",
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
        f"/v0/logs/metric/mean?project={project_name}",
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
        f"/v0/logs/metric/mean?project={project_name}",
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
            f"/v0/logs/metric/{metric}?project={project_name}",
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
async def test_get_logs_metric_batched_with_grouping(client: AsyncClient):
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
            "project": project_name,
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
            "project": project_name,
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
async def test_get_logs_metric_shared_value_reduction(client: AsyncClient):
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
        f"/v0/logs/metric/mean?project={project_name}",
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
        f"/v0/logs/metric/mean?project={project_name}",
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
        f"/v0/logs/metric/mean?project={project_name}",
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
        f"/v0/logs/metric/mean?project={project_name}",
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

    # Test 5: Verify shared value reduction works for all metrics on Group A's score
    for metric in ["sum", "min", "max", "median"]:
        response = await client.get(
            f"/v0/logs/metric/{metric}?project={project_name}",
            params={
                "key": "score",
                "group_by": "entries/group",
            },
            headers=HEADERS,
        )
        assert response.status_code == 200
        result = response.json()

        # For Group A, all scores are 10, so all metrics should return 10
        assert (
            result["A"]["shared_value"] == 10
        ), f"Group A should return the shared value 10 directly for metric {metric}"
