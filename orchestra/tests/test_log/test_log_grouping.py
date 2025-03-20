import json

import pytest
from httpx import AsyncClient

from . import (
    HEADERS,
    _create_derived_entry,
    _create_log,
    _create_logs_for_group_threshold,
    _create_logs_for_grouping,
    _create_project,
    _create_several_logs,
    _delete_logs,
)


@pytest.mark.anyio
async def test_get_logs_groups_project_not_found(client: AsyncClient):
    project_name = "non_existent_project"

    # This should return 404 as the project does not exist
    response = await client.get(
        f"/v0/logs/groups?project={project_name}&key=input",
        headers=HEADERS,
    )

    assert response.status_code == 404, response.json()
    assert response.json() == {
        "detail": f"Project {project_name} not found.",
    }


@pytest.mark.anyio
async def test_get_logs_with_group_threshold(client: AsyncClient):
    project_name = "group-threshold-test"
    _ = await _create_project(client, project_name)
    await _create_logs_for_group_threshold(client, project_name)

    # Test without group_threshold (default behavior)
    response = await client.get(
        f"/v0/logs?project={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    result = response.json()
    assert len(result["logs"]) == 4
    assert "grouped_entries" not in result

    # Test with group_threshold=1 (should group all values)
    response = await client.get(
        f"/v0/logs?project={project_name}&group_threshold=1",
        headers=HEADERS,
    )
    assert response.status_code == 200
    result = response.json()
    assert "grouped_entries" in result
    # All fields should be in grouped_entries since threshold=1
    assert len(result["grouped_entries"]) == 6  # All fields from the log table
    # Check specific values are mapped correctly
    assert "shared_string" in result["grouped_entries"]
    assert "common value" in result["grouped_entries"]["shared_string"].values()
    assert "shared_number" in result["grouped_entries"]
    assert 42 in result["grouped_entries"]["shared_number"].values()
    # Verify logs have shared_entries and no regular entries
    for log in result["logs"]:
        assert "shared_entries" in log
        assert len(log["entries"]) == 0

    # Test with group_threshold=2 (should group values appearing twice or more)
    response = await client.get(
        f"/v0/logs?project={project_name}&group_threshold=2",
        headers=HEADERS,
    )
    assert response.status_code == 200
    result = response.json()
    assert "grouped_entries" in result
    # These fields have values appearing 2+ times
    assert (
        "shared_string" in result["grouped_entries"]
    )  # "common value" appears 4 times
    assert "shared_number" in result["grouped_entries"]  # 42 appears 4 times
    assert (
        "shared_object" in result["grouped_entries"]
    )  # {"key": "value"} appears 4 times
    assert "mixed_field" in result["grouped_entries"]  # "appears twice" appears 2 times
    # These shouldn't be grouped as their values are unique
    assert "unique_string" not in result["grouped_entries"]
    assert "unique_number" not in result["grouped_entries"]

    # Test with group_threshold=4 (should only group values appearing in all logs)
    response = await client.get(
        f"/v0/logs?project={project_name}&group_threshold=4",
        headers=HEADERS,
    )
    assert response.status_code == 200
    result = response.json()
    assert "grouped_entries" in result
    # Only fields with values appearing in all 4 logs should be grouped
    assert "shared_string" in result["grouped_entries"]
    assert "shared_number" in result["grouped_entries"]
    assert "shared_object" in result["grouped_entries"]
    # These shouldn't be grouped as they don't appear in all logs
    assert "mixed_field" not in result["grouped_entries"]
    assert "unique_string" not in result["grouped_entries"]
    assert "unique_number" not in result["grouped_entries"]

    # Test with group_threshold exceeding number of logs (no grouping)
    response = await client.get(
        f"/v0/logs?project={project_name}&group_threshold=5",
        headers=HEADERS,
    )
    assert response.status_code == 200
    result = response.json()
    assert "grouped_entries" not in result
    # All entries should remain in the logs
    for log in result["logs"]:
        assert "shared_entries" not in log
        assert len(log["entries"]) == 6  # All 6 fields should be present

    # Test with empty logs
    _ = await _delete_logs(client, [([1, 2, 3, 4], None)], project_name=project_name)
    response = await client.get(
        f"/v0/logs?project={project_name}&group_threshold=1",
        headers=HEADERS,
    )
    assert response.status_code == 200
    result = response.json()
    assert len(result["logs"]) == 0
    assert "grouped_entries" not in result


@pytest.mark.anyio
async def test_get_log_groups(client: AsyncClient):
    project_name = "eval-project"
    _ = await _create_project(client, project_name)
    _ = await _create_logs_for_grouping(client, project_name)

    # fetch log groups for a given key (params)
    response = await client.get(
        f"/v0/logs/groups?project={project_name}&key=system_prompt",
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    groups = response.json()
    assert isinstance(groups, dict)  # Ensure it's a dict of grouped logs
    assert len(groups) == 2
    assert groups == {
        "0": "You are an expert mathematician.",
        "1": "Respond only with a single digit.",
    }

    # fetch log groups for a given key (entries)
    response = await client.get(
        f"/v0/logs/groups?project={project_name}&key=a/input",
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    groups = response.json()
    assert isinstance(groups, dict)  # Ensure it's a dict of grouped logs
    assert len(groups) == 2
    assert groups == {
        "0": "What is 2 + 2?",
        "1": "What is 1 + 1?",
    }


@pytest.mark.anyio
async def test_get_log_groups_combined(client: AsyncClient):
    project_name = "eval-project"
    _ = await _create_project(client, project_name)
    _ = await _create_logs_for_grouping(client, project_name)

    # Test filtering by system_prompt
    response = await client.get(
        f"/v0/logs/groups?project={project_name}&key=system_prompt&filter_expr=len(a/input) > 10",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    groups = response.json()
    assert isinstance(groups, dict)
    assert len(groups) == 2
    assert groups == {
        "0": "You are an expert mathematician.",
        "1": "Respond only with a single digit.",
    }

    # Test with no matching logs after filtering
    response = await client.get(
        f"/v0/logs/groups?project={project_name}&key=system_prompt&filter_expr=a/input == 'nonexistent'",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    groups = response.json()
    assert isinstance(groups, dict)
    assert len(groups) == 0

    # Get log IDs
    response = await client.get(
        f"/v0/logs?project={project_name}&return_ids_only=true",
        headers=HEADERS,
    )
    assert response.status_code == 200
    log_ids = response.json()

    # Test with subset of log IDs
    selected_ids = log_ids[:2]
    response = await client.get(
        f"/v0/logs/groups?project={project_name}",
        params={
            "key": "system_prompt",
            "from_ids": "&".join([str(i) for i in selected_ids]),
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    groups = response.json()
    assert isinstance(groups, dict)
    assert len(groups) == 1
    assert groups == {
        "1": "Respond only with a single digit.",
    }

    # Test excluding some log IDs
    exclude_ids = log_ids[:2]
    response = await client.get(
        f"/v0/logs/groups?project={project_name}",
        params={
            "key": "system_prompt",
            "exclude_ids": "&".join([str(i) for i in exclude_ids]),
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    groups = response.json()
    assert isinstance(groups, dict)
    assert len(groups) == 1
    assert groups == {
        "0": "You are an expert mathematician.",
    }


@pytest.mark.anyio
@pytest.mark.skip(reason="Skipping test due to response structure changes")
async def test_get_logs_grouping_all_scenarios(client: AsyncClient):
    # Test for the following:
    # - Single-level grouping (entries & params)
    # - Multi-level grouping
    # - group_offset / group_limit
    # - group_depth
    # - group_sorting

    project_name = "test-grouping-comprehensive"
    _ = await _create_project(client, project_name)

    # 1) Create initial logs using your existing fixture
    await _create_several_logs(client, project_name, batched=False)

    # Create derived logs for testing grouping
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

    # 2) Create *additional* logs so we can test grouping by params properly.
    #    We'll vary the version param "a/b/param2" across logs.
    custom_logs_for_param_versions = [
        {
            "params": {"a/b/param1": "extra_test_1", "a/b/param2": "0"},
            "entries": {
                "_/description": "param-version log #1",
                "_/state": "extra_liquid",
                "_/safe": True,
            },
        },
        {
            "params": {"a/b/param1": "extra_test_2", "a/b/param2": "1"},
            "entries": {
                "_/description": "param-version log #2",
                "_/state": "extra_liquid",
                "_/safe": False,
            },
        },
        {
            "params": {"a/b/param1": "extra_test_3", "a/b/param2": "1"},
            "entries": {
                "_/description": "param-version log #3",
                "_/state": "extra_vapor",
                "_/safe": True,
            },
        },
    ]
    for item in custom_logs_for_param_versions:
        response = await client.post(
            "/v0/logs",
            json={
                "project": project_name,
                "params": item["params"],
                "entries": item["entries"],
            },
            headers=HEADERS,
        )
        assert response.status_code == 200, response.json()

    #
    # ==========  SCENARIO 1: Single-level grouping by "entries/_/state"  ==========
    #
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={
            "group_by": ["entries/_/state"],
        },
        headers=HEADERS,
    )

    assert response.status_code == 200
    result = response.json()
    logs_section = result["logs"]  # e.g. "logs": { "entries/_/state": { ... } }

    # Make sure the top-level dict has exactly 1 key: "entries/_/state"
    assert len(logs_section) == 1, f"Expected 1 group key, found: {logs_section.keys()}"
    root_key = list(logs_section.keys())[0]
    assert root_key == "entries/_/state"

    group_obj = logs_section["entries/_/state"]
    assert "group_count" in group_obj
    assert "count" in group_obj

    assert (
        group_obj["count"] == 10
    ), "We expect 10 total logs across all states (including null)."

    # Check that the 'null' group is present if we have logs that do not have `_/state`.
    # In your snippet, logs with event_id=5,6,7 do not have `_/state`, so we expect "null".
    group_keys = [k for k in group_obj.keys() if k not in ("group_count", "count")]
    assert (
        "null" in group_keys
    ), "We expect a 'null' group for logs that have no _/state field."
    assert "extra_liquid" in group_keys
    assert "extra_vapor" in group_keys
    assert "gas" in group_keys
    assert "liquid->solid" in group_keys
    assert "liquid->gas" in group_keys

    # Now check each group is either a list (leaf) or a sub-dict if we had more grouping
    for key in group_keys:
        sub = group_obj[key]
        if isinstance(sub, list):
            # Leaf logs
            for log in sub:
                assert "id" in log
                assert "ts" in log
                # etc.
        else:
            # If for some reason there's a nested grouping
            # But this is a single-level grouping, so it should be a list
            raise AssertionError(f"Expected a leaf list for {key}, got {type(sub)}")

    # Check for derived entries in grouped results
    for state_val, logs_list in group_obj.items():
        if state_val not in ("group_count", "count"):
            for log in logs_list:
                if log["id"] in [1, 2, 3, 4]:
                    assert (
                        "derived_temp" in log["derived_entries"]
                    ), f"Missing derived_temp in log {log['id']}"
                    assert (
                        "state_len" in log["derived_entries"]
                    ), f"Missing state_len in log {log['id']}"
                    # Verify derived values are correct
                    if "_/temperature" in log["entries"]:
                        assert (
                            log["derived_entries"]["derived_temp"]
                            == log["entries"]["_/temperature"] + 10
                        )
                    if "_/state" in log["entries"]:
                        assert log["derived_entries"]["state_len"] == len(
                            log["entries"]["_/state"],
                        )

    #
    # ==========  SCENARIO 2: Single-level grouping by param "a/b/param1"  ==========
    #
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"group_by": ["params/a/b/param1"]},
        headers=HEADERS,
    )
    assert response.status_code == 200
    result = response.json()
    logs_section = result.get("logs", {})

    # Check top level structure
    assert len(logs_section) == 1, f"Expected 1 group key, found: {logs_section.keys()}"
    assert "params/a/b/param1" in logs_section

    param1_groups = logs_section["params/a/b/param1"]
    assert "group_count" in param1_groups
    assert "count" in param1_groups
    assert param1_groups["count"] == 10, "Expected 10 total logs"

    # Check group keys - we should have test_0 through test_6 and extra_test_1 through extra_test_3
    group_keys = [k for k in param1_groups.keys() if k not in ("group_count", "count")]
    assert len(group_keys) >= 9, "Expected at least 9 distinct param1 values"

    # Verify each group contains valid logs
    for param1_val in group_keys:
        group_logs = param1_groups[param1_val]
        assert isinstance(group_logs, list), f"Expected list for param1={param1_val}"
        for log in group_logs:
            assert "id" in log
            assert "ts" in log
            assert "entries" in log
            assert "params" in log
            # The grouped-by field should be removed from params
            assert "a/b/param1" not in log["params"]

    # Verify derived entries are preserved when grouping by params
    for param_val, logs_list in param1_groups.items():
        if param_val not in ("group_count", "count"):
            for log in logs_list:
                if log["id"] in [1, 2, 3, 4]:
                    assert (
                        "derived_temp" in log["derived_entries"]
                    ), f"Missing derived_temp in log {log['id']}"
                    assert (
                        "state_len" in log["derived_entries"]
                    ), f"Missing state_len in log {log['id']}"
    #
    # ==========  SCENARIO 3: Multi-level grouping by param "a/b/param2" and "entries/_/state"  ==========
    #
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"group_by": ["params/a/b/param2", "entries/_/state"]},
        headers=HEADERS,
    )
    assert response.status_code == 200
    result = response.json()

    logs_section = result["logs"]
    assert len(logs_section) == 1
    root_key = list(logs_section.keys())[0]
    assert root_key == "params/a/b/param2"

    top_level = logs_section["params/a/b/param2"]
    assert "group_count" in top_level
    assert "count" in top_level
    assert top_level["count"] == 10, "Should still be 10 logs total at top level."

    # Distinct param2 values might be "0", "1", plus "null" if some logs lack param2
    top_keys = [k for k in top_level.keys() if k not in ("group_count", "count")]
    assert (
        "null" in top_keys
    ), "We do have logs that lack param2 (IDs 1..7), so expect 'null'."

    # For each version => sub-dict "entries/_/state"
    for version_val in top_keys:
        sub_obj = top_level[version_val]
        # This should have exactly 1 key: "entries/_/state"
        assert len(sub_obj) == 1 or (
            len(sub_obj) in (2, 3) and "group_count" in sub_obj
        ), f"Expected a single group key, found: {sub_obj.keys()}"
    second_level_key = list(sub_obj.keys())[0]
    assert second_level_key == "entries/_/state"
    second_level = sub_obj["entries/_/state"]
    assert "group_count" in second_level
    assert "count" in second_level

    # Then each distinct state is either a list or a further dict if you had more grouping
    for st_key, st_val in second_level.items():
        if st_key in ("group_count", "count"):
            continue
        if isinstance(st_val, list):
            # leaf logs
            for log in st_val:
                assert "id" in log
                assert "ts" in log
                # etc.
        else:
            # Could be a deeper group if we had a third dimension
            pass

    # Verify derived entries are preserved in multi-level grouping
    param2_groups = logs_section["params/a/b/param2"]
    for param2_val, state_groups in param2_groups.items():
        if param2_val not in ("group_count", "count"):
            state_level = state_groups["entries/_/state"]
            for state_val, logs_list in state_level.items():
                if state_val not in ("group_count", "count"):
                    for log in logs_list:
                        if log["id"] in [1, 2, 3, 4]:
                            assert "derived_temp" in log["derived_entries"]
                            assert "state_len" in log["derived_entries"]

    # Verify derived entries are preserved when grouping by params
    # ==========  SCENARIO 4: Group pagination (group_limit, group_offset)  ==========
    #
    # We'll do it on the "entries/_/state" grouping, which we know has at least 5 distinct states.
    # Example: group_limit=2, group_offset=1 => we skip the first group, only show the next 2
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={
            "group_by": ["entries/_/state"],
            "group_limit": 2,
            "group_offset": 1,
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    result = response.json()

    # Check top level structure
    logs_section = result["logs"]
    assert len(logs_section) == 1
    assert "entries/_/state" in logs_section

    state_groups = logs_section["entries/_/state"]
    assert "group_count" in state_groups
    assert "count" in state_groups
    total_groups = state_groups["group_count"]
    assert total_groups == 5, "Expected 5 total state groups"
    assert state_groups["count"] == 7, "Expected 7 total logs (including null)"

    # Check pagination results (2 groups after pagination +  1 null group (default))
    returned_groups = [
        k for k in state_groups.keys() if k not in ("group_count", "count")
    ]
    assert (
        len(returned_groups) == 3
    ), f"Expected exactly 3 groups with limit=2, got {len(returned_groups)}"
    assert "null" in returned_groups, "Expected a 'null' group"

    # Verify each returned group contains valid logs
    for state_val in returned_groups:
        group_logs = state_groups[state_val]
        assert isinstance(group_logs, list), f"Expected list for state={state_val}"
        for log in group_logs:
            assert "id" in log
            assert "ts" in log
            assert "entries" in log
            assert "params" in log
            # The state field should be removed from entries since it's grouped
            assert "_/state" not in log["entries"]

    #
    # ==========  SCENARIO 5: Group depth tests  ==========
    #
    for depth in [0, 1, 2, 3, 4]:
        response = await client.get(
            f"/v0/logs?project={project_name}",
            params={
                "group_by": ["params/a/b/param2", "entries/_/state", "entries/_/safe"],
                "group_depth": depth,
            },
            headers=HEADERS,
        )
        assert response.status_code == 200
        result = response.json()

        logs_section = result["logs"]
        assert len(logs_section) == 1
        assert "params/a/b/param2" in logs_section
        param2_groups = logs_section["params/a/b/param2"]

        if depth == 0:
            # With group_depth=0 the top‐level grouping is cut off:
            # Each distinct param2 value is mapped directly to an integer count.
            assert "group_count" in param2_groups
            assert "count" in param2_groups
            assert param2_groups["count"] == 3

            # For every group key (other than metadata) we expect an integer
            for k, v in param2_groups.items():
                if k not in ("group_count", "count"):
                    assert isinstance(
                        v,
                        int,
                    ), f"Expected integer count for param2={k}, got {type(v)}"

        elif depth == 1:
            # With group_depth=1 the first level (param2) is expanded,
            # but the next level (state) is collapsed into counts.
            assert "group_count" in param2_groups
            assert "count" in param2_groups
            assert param2_groups["count"] == 7

            # Now each param2 value should map to a dict
            for key in ("1", "0", "null"):
                assert isinstance(
                    param2_groups[key],
                    dict,
                ), f"Expected dict for key {key}"

            # Check that the state groups have the expected counts:
            state_for_1 = param2_groups["1"]
            assert state_for_1.get("group_count") == 2
            assert state_for_1.get("count") == 2
            assert state_for_1.get("extra_vapor") == 1
            assert state_for_1.get("extra_liquid") == 1

            state_for_0 = param2_groups["0"]
            assert state_for_0.get("group_count") == 1
            assert state_for_0.get("count") == 1
            assert state_for_0.get("extra_liquid") == 1

            state_for_null = param2_groups["null"]
            assert state_for_null.get("group_count") == 4
            assert state_for_null.get("count") == 4
            assert state_for_null.get("liquid->solid") == 2
            assert state_for_null.get("gas") == 1
            assert state_for_null.get("liquid->gas") == 1
            assert state_for_null.get("null") == 0

            # Only these keys (plus metadata) should be present at the param2 level:
            expected_keys = {"1", "0", "null", "group_count", "count"}
            assert set(param2_groups.keys()) == expected_keys

        elif depth == 2:
            # With group_depth=2 the top-level param2 groups are expanded,
            # and now the state groups (inside each param2 key) are expanded;
            # however, the next level (safe) is collapsed to counts.
            assert "group_count" in param2_groups
            assert "count" in param2_groups
            assert param2_groups["count"] == 8
            assert param2_groups["group_count"] == 2

            for param2_val, state_groups in param2_groups.items():
                if param2_val in ("group_count", "count"):
                    continue
                # Each param2 group must contain an "entries/_/state" key
                assert "entries/_/state" in state_groups
                state_level = state_groups["entries/_/state"]
                assert "group_count" in state_level
                assert "count" in state_level

                if param2_val == "1":
                    # For param2 "1", we expect two state groups: "extra_vapor" and "extra_liquid"
                    ev = state_level["extra_vapor"]
                    assert isinstance(ev, dict)
                    assert ev.get("true") == 1
                    assert ev.get("group_count") == 1
                    assert ev.get("count") == 1

                    el = state_level["extra_liquid"]
                    assert isinstance(el, dict)
                    assert el.get("false") == 1
                    assert el.get("group_count") == 1
                    assert el.get("count") == 1

                    # Overall, the state level for param2 "1" must sum to count 2 with group_count 2
                    assert state_level["count"] == 2
                    assert state_level["group_count"] == 2

                elif param2_val == "0":
                    # For param2 "0", we expect only the "extra_liquid" group
                    el = state_level["extra_liquid"]
                    assert isinstance(el, dict)
                    assert el.get("true") == 1
                    assert el.get("group_count") == 1
                    assert el.get("count") == 1

                    assert state_level["count"] == 1
                    assert state_level["group_count"] == 1

                elif param2_val == "null":
                    # For param2 "null", there are several state groups.
                    ls = state_level["liquid->solid"]
                    assert isinstance(ls, dict)
                    assert ls.get("false") == 1
                    assert ls.get("true") == 1
                    assert ls.get("group_count") == 2
                    assert ls.get("count") == 2

                    gas = state_level["gas"]
                    assert isinstance(gas, dict)
                    assert gas.get("false") == 1
                    assert gas.get("group_count") == 1
                    assert gas.get("count") == 1

                    lg = state_level["liquid->gas"]
                    assert isinstance(lg, dict)
                    assert lg.get("false") == 1
                    assert lg.get("group_count") == 1
                    assert lg.get("count") == 1

                    n = state_level["null"]
                    assert isinstance(n, dict)
                    assert n.get("null") == 3
                    assert n.get("group_count") == 1
                    assert n.get("count") == 1

                    # Overall, state level for param2 "null" must have count 7 and group_count 3.
                    assert state_level["count"] == 5
                    assert state_level["group_count"] == 3

        elif depth >= 3:
            # With group_depth>=3 all levels are fully expanded to log lists.
            # That is, inside the state groups the safe groups are no longer counts but full lists of logs.
            assert "group_count" in param2_groups
            assert "count" in param2_groups
            assert param2_groups["count"] == 10
            assert param2_groups["group_count"] == 2

            for param2_val, state_groups in param2_groups.items():
                if param2_val in ("group_count", "count"):
                    continue
                assert "entries/_/state" in state_groups
                state_level = state_groups["entries/_/state"]
                assert "group_count" in state_level
                assert "count" in state_level

                for state_val, safe_groups in state_level.items():
                    if state_val in ("group_count", "count"):
                        continue
                    assert "entries/_/safe" in safe_groups
                    safe_level = safe_groups["entries/_/safe"]
                    assert "group_count" in safe_level
                    assert "count" in safe_level

                    # Each safe value should now be a list of logs
                    for safe_val, logs in safe_level.items():
                        if safe_val in ("group_count", "count"):
                            continue
                        assert isinstance(
                            logs,
                            list,
                        ), f"Expected list of logs for safe={safe_val}"
                        for log in logs:
                            assert "id" in log
                            assert "ts" in log
                            assert "entries" in log
                            assert "params" in log
                            # Grouped fields should be stripped from the leaf logs
                            assert "a/b/param2" not in log["params"]
                            assert "_/state" not in log["entries"]
                            assert "_/safe" not in log["entries"]

        # ==========  SCENARIO 6: Group by + sort_across_groups  ==========
        response = await client.get(
            "/v0/logs",
            params={
                "project": project_name,
                "group_by": ["entries/_/state"],
                "group_sorting": json.dumps(
                    {
                        "entries/_/state": {
                            "field": "derived_temp",
                            "metric": "mean",
                            "direction": "descending",
                        },
                    },
                ),
            },
            headers=HEADERS,
        )
        assert response.status_code == 200
        result = response.json()

        # Extract the top-level group object:
        logs_section = result["logs"]
        assert (
            "entries/_/state" in logs_section
        ), "Expected a top-level grouping by 'entries/_/state'"
        group_obj = logs_section["entries/_/state"]
        assert "group_count" in group_obj and "count" in group_obj

        # Get all actual group names (excluding metadata like "count" and "group_count")
        group_keys = [k for k in group_obj.keys() if k not in ("count", "group_count")]

        # For each group, compute the average derived_temp among its logs (if any).
        def compute_mean_derived_temp(logs_list):
            vals = []
            for log_item in logs_list:
                dt = log_item["derived_entries"].get("derived_temp")
                if dt is not None:
                    vals.append(dt)
            return sum(vals) / len(vals) if vals else float("inf")  # or 0 if you prefer

        grouped_averages = []
        for gk in group_keys:
            # Each group here should be a list of logs (leaf level).
            group_logs = group_obj[gk]
            if not isinstance(group_logs, list):
                continue
            avg_temp = compute_mean_derived_temp(group_logs)
            grouped_averages.append((gk, avg_temp))

        # Verify the groups are sorted in descending order by mean(derived_temp)
        for i in range(len(grouped_averages) - 1):
            if grouped_averages[i + 1][0] == "null":
                continue
            else:
                assert grouped_averages[i][1] >= grouped_averages[i + 1][1], (
                    f"Groups are not in descending order by derived_temp mean: "
                    f"{grouped_averages[i]} vs {grouped_averages[i+1]}"
                )


@pytest.mark.anyio
@pytest.mark.skip(reason="Skipping test due to response structure changes")
async def test_sorting_with_grouping(client: AsyncClient):
    """Test sorting functionality within groups and across groups."""
    project_name = "test-sorting-with-grouping"
    await _create_project(client, project_name)

    # Create test data: student scores across different tests
    test_data = [
        {"student": "Alice", "test": "Math", "score": 95},
        {"student": "Alice", "test": "Physics", "score": 88},
        {"student": "Alice", "test": "Chemistry", "score": 92},
        {"student": "Bob", "test": "Math", "score": 82},
        {"student": "Bob", "test": "Physics", "score": 90},
        {"student": "Bob", "test": "Chemistry", "score": 85},
        {"student": "Charlie", "test": "Math", "score": 78},
        {"student": "Charlie", "test": "Physics", "score": 75},
        {"student": "Charlie", "test": "Chemistry", "score": 80},
    ]

    # Create logs for each test score
    for entry in test_data:
        response = await _create_log(client, project_name, entries=entry)
        assert response.status_code == 200, response.json()

    #
    # TEST 1: Sort within groups (group_by=student, normal "sorting")
    # We expect: "logs" -> { "entries/student": { "Alice": [..], "Bob": [..], ...,
    #                                           "group_count": 3, "count": 9 } }
    #
    response = await client.get(
        "/v0/logs",
        params={
            "project": project_name,
            "group_by": ["entries/student"],
            "sorting": json.dumps({"score": "descending"}),
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    result = response.json()

    # The grouped data should be in result["logs"]["entries/student"]
    logs_section = result["logs"]
    assert isinstance(logs_section, dict), "Expected a dict for logs"

    group_obj = logs_section["entries/student"]
    assert "group_count" in group_obj, "Missing 'group_count' in group-level dict"
    assert "count" in group_obj, "Missing 'count' in group-level dict"

    # Here we specifically look for each known student by name
    # and verify each group's logs are sorted (descending) by "score".
    for student in ["Alice", "Bob", "Charlie"]:
        # group_obj[student] should be a list of logs
        logs_list = group_obj[student]
        scores = [log["entries"]["score"] for log in logs_list]
        assert scores == sorted(
            scores,
            reverse=True,
        ), f"Scores not properly sorted in descending order for {student}"

        # Confirm the exact descending sequence:
        if student == "Alice":
            assert scores == [95, 92, 88], "Alice's scores not in correct order"
        elif student == "Bob":
            assert scores == [90, 85, 82], "Bob's scores not in correct order"
        elif student == "Charlie":
            assert scores == [80, 78, 75], "Charlie's scores not in correct order"

    #
    # TEST 2: Sort across groups via "sort_across_groups" (aggregator = mean of 'score')
    #
    response = await client.get(
        "/v0/logs",
        params={
            "project": project_name,
            "group_by": ["entries/student"],
            "group_sorting": json.dumps(
                {
                    "entries/student": {
                        "field": "score",
                        "metric": "mean",
                        "direction": "descending",
                    },
                },
            ),
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    result = response.json()

    # The grouped data is still under result["logs"]["entries/student"],
    # but now the *order* of the group keys is aggregator-based.
    logs_section = result["logs"]["entries/student"]
    group_keys = [
        k
        for k in logs_section.keys()
        if k not in ("group_count", "count", "_aggregator_metric")
    ]

    # We'll compute each group's mean ourselves to verify ordering
    def mean(lst):
        return sum(lst) / len(lst) if lst else float("nan")

    mean_map = {}
    for student in group_keys:
        logs_list = logs_section[student]
        sc = [log["entries"]["score"] for log in logs_list if "score" in log["entries"]]
        mean_map[student] = mean(sc)

    # The groups appear in dictionary order by default in Python 3.7+,
    # but we can see them in the returned JSON in a certain order. We'll
    # parse them in that sequence:
    # E.g. group_keys might be ["Alice", "Bob", "Charlie"] or any order
    # We'll compare group_keys to the sorted descending sequence by mean_map
    descending_students = sorted(mean_map, key=lambda s: mean_map[s], reverse=True)
    assert group_keys == descending_students, (
        "Groups not sorted by aggregator mean(score) in descending order. "
        f"Expected {descending_students}, got {group_keys}"
    )

    # For these students:
    #  - Alice's mean = (95 + 88 + 92) / 3 = 91.666..
    #  - Bob's   mean = (82 + 90 + 85) / 3 = 85.666..
    #  - Charlie's = (78 + 75 + 80) / 3 = 77.666..
    # So we expect ["Alice", "Bob", "Charlie"] in that order
    assert group_keys == ["Alice", "Bob", "Charlie"], "Unexpected group order"


@pytest.mark.anyio
@pytest.mark.skip(reason="Skipping test due to response structure changes")
async def test_sorting_edge_cases(client: AsyncClient):
    """Test edge cases in sorting with groups."""
    project_name = "test-sorting-edge-cases"
    await _create_project(client, project_name)

    # Create test data with edge cases
    test_data = [
        {"student": "Alice", "test": "Math", "score": None},  # Null score
        {"student": "Alice", "test": "Physics", "score": 88},
        {"student": "Bob", "test": "Math", "score": 82},
        {"student": "Bob", "test": "Physics"},  # Missing score field
        {"student": "Charlie", "test": "Math", "score": 0},  # Zero score
        {"student": "Charlie", "test": "Physics", "score": -5},  # Negative score
    ]
    for entry in test_data:
        response = await _create_log(client, project_name, entries=entry)
        assert response.status_code == 200, response.json()

    # 1) Sort across groups with null or missing score fields
    #    We group by 'student' and want to see which group is highest in mean(score)
    response = await client.get(
        "/v0/logs",
        params={
            "project": project_name,
            "group_by": ["entries/student"],
            "group_sorting": json.dumps(
                {
                    "entries/student": {
                        "field": "score",
                        "metric": "mean",
                        "direction": "descending",
                    },
                },
            ),
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    result = response.json()

    # The grouped data is under result["logs"]["entries/student"]
    groups_dict = result["logs"]["entries/student"]
    group_names = [
        k
        for k in groups_dict.keys()
        if k not in ("count", "group_count", "_aggregator_metric")
    ]

    # Compute the mean of scores for each group
    def safe_mean(logs_list):
        vals = []
        for lg in logs_list:
            if "score" in lg["entries"]:
                sc = lg["entries"].get("score", None)
                # if missing or None, skip or treat as 0
                if sc is None:
                    vals.append(0)
                else:
                    vals.append(sc)
        return sum(vals) / len(vals) if vals else float("-inf")

    mean_map = {gn: safe_mean(groups_dict[gn]) for gn in group_names}

    # Make sure the group_names are sorted desc by that mean
    sorted_desc = sorted(mean_map, key=lambda x: mean_map[x], reverse=True)
    assert group_names == sorted_desc, (
        f"Groups not sorted descending by mean score. "
        f"Got group order={group_names}, expected={sorted_desc}"
    )

    # 2) Sort within groups
    response = await client.get(
        "/v0/logs",
        params={
            "project": project_name,
            "group_by": ["entries/student"],
            "sorting": json.dumps({"score": "descending"}),
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    result = response.json()

    groups_dict = result["logs"]["entries/student"]
    # Check each group to ensure logs are sorted descending by "score",
    # with None or missing fields placed last if your logic does so.
    for student, logs_list in groups_dict.items():
        if student in ("count", "group_count", "_aggregator_metric"):
            continue
        # We expect logs_list is a list
        actual_scores = [lg["entries"].get("score", None) for lg in logs_list]
        # Typically, null or missing appear at the end
        # We'll just check that all numeric scores are in descending order
        numeric_scores = [x for x in actual_scores if isinstance(x, (int, float))]
        # find the first None in the list
        idx_first_null = next((i for i, v in enumerate(actual_scores) if v is None), -1)
        # confirm numeric part is descending
        assert numeric_scores == sorted(numeric_scores, reverse=True), (
            f"Scores not in descending order for {student}. " f"Got {actual_scores}"
        )
        if idx_first_null != -1:
            # everything after idx_first_null should be None
            for v in actual_scores[idx_first_null:]:
                assert (
                    v is None
                ), f"Non-null score {v} found after first null in {actual_scores}"


@pytest.mark.anyio
@pytest.mark.skip(reason="Skipping test due to response structure changes")
async def test_nested_group_sorting_with_separate_metrics(client: AsyncClient):
    """
    Scenario: We have two grouping fields: ["entries/country", "entries/student"].
    We also have a 'score' field. We want to:
       - Sort each 'country' group by the SUM of scores (descending).
       - Within each country, sort 'student' groups by the MEAN of scores (descending).
    """

    project_name = "test-nested-separate-metrics"
    await _create_project(client, project_name)

    # Insert sample data
    data = [
        ("USA", "Alice", 95),
        ("USA", "Alice", 85),
        ("USA", "Bob", 70),
        ("USA", "Bob", 72),
        ("Canada", "Alice", 88),
        ("Canada", "Charlie", 90),
        ("Canada", "Charlie", 82),
        ("Mexico", "Diana", 100),
        ("Mexico", "Diana", 100),
        ("Mexico", "Bob", 60),
    ]
    for country, student, score in data:
        r = await client.post(
            "/v0/logs",
            json={
                "project": project_name,
                "entries": {"country": country, "student": student, "score": score},
            },
            headers=HEADERS,
        )
        assert r.status_code == 200

    # group_sorting config:
    #  - "entries/country": sum of 'score' => descending
    #  - "entries/student": mean of 'score' => descending
    group_sorting = {
        "entries/country": {
            "field": "score",
            "direction": "descending",
            "metric": "sum",
        },
        "entries/student": {
            "field": "score",
            "direction": "descending",
            "metric": "mean",
        },
    }

    resp = await client.get(
        "/v0/logs",
        params={
            "project": project_name,
            "group_by": ["entries/country", "entries/student"],
            "group_sorting": json.dumps(group_sorting),
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200
    result = resp.json()

    # Check shape:
    #   result["logs"] -> { "entries/country": { <country1>: { "entries/student": {...}, "_agg_value": 123.0 },
    #                                           <country2>: {...},
    #                                           "group_count": X, "count": Y, "_agg_value": ??? } }
    logs_section = result["logs"]
    assert "entries/country" in logs_section

    countries_obj = logs_section["entries/country"]
    group_keys = [
        k
        for k in countries_obj.keys()
        if k not in ("group_count", "count", "_aggregator_metric")
    ]

    # We'll compute the sum of scores for each country from `data` to confirm the ordering
    from collections import defaultdict

    sums_by_country = defaultdict(float)
    for c, s, sc in data:
        sums_by_country[c] += sc

    # Sort the countries by their total sum desc
    expected_country_order = sorted(
        sums_by_country.keys(),
        key=lambda c: sums_by_country[c],
        reverse=True,
    )

    # e.g. Mexico => 260, USA => 322, Canada => 262
    # Let's compute them precisely:
    #  - USA: 95+85+70+72 = 322
    #  - Canada: 88+90+82 = 260
    #  - Mexico: 100+100+60 = 260
    # So actually, USA=322, Canada=260, Mexico=260 (tie between Canada & Mexico).
    # We'll accept either "Canada, Mexico" or "Mexico, Canada" as valid if they have the same sum.

    actual_country_order = []
    for k in group_keys:
        # Each top-level group key is "USA", "Canada", or "Mexico"
        actual_country_order.append(k)

    # Check that first is "USA" since it definitely has highest sum=322
    assert (
        actual_country_order[0] == "USA"
    ), f"Expected 'USA' first, got {actual_country_order}"

    # The second and third can be "Canada" or "Mexico" in either order if they tie at 260
    # We'll verify they are just some permutation of ("Canada", "Mexico")
    assert sorted(actual_country_order[1:3]) == [
        "Canada",
        "Mexico",
    ], f"Unexpected order for {actual_country_order}"

    # Now test each country's child grouping => 'entries/student' with mean sorting
    for country in actual_country_order:
        sub_dict = countries_obj[country]
        assert (
            "entries/student" in sub_dict
        ), f"Missing student-level grouping under country={country}"
        students_obj = sub_dict["entries/student"]
        student_keys = [
            k
            for k in students_obj.keys()
            if k not in ("group_count", "count", "_aggregator_metric")
        ]
        # gather each student's logs => compute mean
        # sub_dict for each student should be a list or nested structure. In your example, it's typically a leaf.

        # Build a map from student -> (list_of_scores, mean_of_scores)
        from statistics import mean

        student_score_map = defaultdict(list)
        for st in student_keys:
            if st in ("group_count", "count", "_aggregator_metric"):
                continue
            # child might be a list of logs or a dict with "_leaf_logs"
            child_val = students_obj[st]
            # If it's a leaf, maybe child_val["_leaf_logs"] is the actual logs
            if isinstance(child_val, dict) and "_leaf_logs" in child_val:
                logs_list = child_val["_leaf_logs"]
            elif isinstance(child_val, list):
                logs_list = child_val  # depends on your actual shape
            else:
                raise AssertionError(f"Unexpected shape for {st} => {child_val}")

            scores = [
                lg["entries"]["score"] for lg in logs_list if "score" in lg["entries"]
            ]
            student_score_map[st] = scores

        # Now read them in the actual order they appear
        actual_student_order = []
        for st in student_keys:
            actual_student_order.append(st)

        # They should be sorted descending by mean
        def get_mean(st):
            scs = student_score_map[st]
            return mean(scs) if scs else 0.0

        # Check each consecutive pair
        for i in range(len(actual_student_order) - 1):
            m1 = get_mean(actual_student_order[i])
            m2 = get_mean(actual_student_order[i + 1])
            assert m1 >= m2, (
                f"Students not in descending order by mean score. {actual_student_order[i]} has mean={m1}, "
                f"{actual_student_order[i+1]} has mean={m2}"
            )


@pytest.mark.anyio
@pytest.mark.skip(reason="Skipping test due to response structure changes")
async def test_nested_group_sorting_leaf_only(client: AsyncClient):
    """
    Same data, but we only specify 'group_sorting' for the *leaf* 'entries/student'.
    The top-level 'entries/country' is left unsorted (no aggregator).
    """

    project_name = "test-nested-leaf-only"
    await _create_project(client, project_name)

    data = [
        ("USA", "Alice", 95),
        ("USA", "Alice", 85),
        ("Canada", "Charlie", 90),
        ("Canada", "Alice", 75),
        ("Mexico", "Bob", 50),
        ("Mexico", "Bob", 40),
        ("Mexico", "Diana", 100),
    ]
    for country, student, score in data:
        r = await client.post(
            "/v0/logs",
            json={
                "project": project_name,
                "entries": {"country": country, "student": student, "score": score},
            },
            headers=HEADERS,
        )
        assert r.status_code == 200

    # group_by => 2 levels, but "entries/country" has no aggregator config.
    # We *only* do aggregator sorting for "entries/student" with mean descending.
    group_sorting = {
        "entries/student": {
            "field": "score",
            "direction": "descending",
            "metric": "mean",
        },
    }
    # No entry for "entries/country": => no sorting across countries

    resp = await client.get(
        "/v0/logs",
        params={
            "project": project_name,
            "group_by": ["entries/country", "entries/student"],
            "group_sorting": json.dumps(group_sorting),
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200
    result = resp.json()

    # Because we did NOT specify aggregator for entries/country,
    # we expect them in the default order (i.e: latest creation time)
    # (like ["Mexico", "Canada", "USA"] if that’s their insertion order).
    logs_section = result["logs"]
    countries_obj = logs_section["entries/country"]
    top_countries = [
        k
        for k in countries_obj.keys()
        if k not in ("group_count", "count", "_aggregator_metric")
    ]

    # We might just check that top_countries is exactly the distinct set, ignoring order:
    expected_countries = {"Mexico", "Canada", "USA"}
    assert (
        set(top_countries) == expected_countries
    ), f"Missing or extra countries: {top_countries}"

    # Now inside each country => we DID specify aggregator for 'entries/student' =>
    # so the *student* subgroups should be sorted by mean descending.
    for c in top_countries:
        sub_obj = countries_obj[c]
        assert "entries/student" in sub_obj
        students_obj = sub_obj["entries/student"]
        child_keys = [
            x
            for x in students_obj
            if x not in ("group_count", "count", "_aggregator_metric")
        ]
        # read each child's logs => compute mean
        from statistics import mean

        student_mean_map = {}
        for st in child_keys:
            if st in ("group_count", "count", "_aggregator_metric"):
                continue
            val = students_obj[st]
            # leaf logs
            if isinstance(val, dict) and "_leaf_logs" in val:
                logs_list = val["_leaf_logs"]
            elif isinstance(val, list):
                logs_list = val
            else:
                raise AssertionError(f"Unexpected shape for {st} => {val}")
            scores = [lg["entries"].get("score", 0) for lg in logs_list]
            student_mean_map[st] = mean(scores) if scores else 0.0

        # The child_keys themselves have a stable order from the JSON
        for i in range(len(child_keys) - 1):
            cur_student = child_keys[i]
            nxt_student = child_keys[i + 1]
            cur_mean = student_mean_map[cur_student]
            nxt_mean = student_mean_map[nxt_student]
            assert cur_mean >= nxt_mean, (
                f"Students not sorted by descending mean in {c} group. "
                f"{cur_student} has mean={cur_mean}, next is {nxt_student} with mean={nxt_mean}"
            )


@pytest.mark.anyio
@pytest.mark.skip(reason="Skipping test due to response structure changes")
async def test_sort_within_and_across_groups_together(client: AsyncClient):
    """
    We group by 'student', sorting those groups across by mean(score) descending,
    but within each group, we sort logs by timestamp ascending.
    """

    project_name = "test-within-and-across-groups"
    await _create_project(client, project_name)

    # Data: 7 logs
    data = [
        # (student, test, score, timestamp)
        ("Alice", "Math", 95, "2025-01-02 10:00:00"),
        ("Alice", "Chem", 92, "2025-01-02 09:59:00"),
        ("Bob", "Math", 82, "2025-01-01 15:00:00"),
        ("Bob", "Chem", 85, "2025-01-01 20:30:00"),
        ("Bob", "Phys", 90, "2025-01-01 21:00:00"),
        ("Charlie", "Math", 78, "2025-01-03 13:00:00"),
        ("Charlie", "Chem", 80, "2025-01-03 12:45:00"),
    ]
    # Insert logs
    for i, (stud, subj, sc, ts) in enumerate(data):
        resp = await client.post(
            "/v0/logs",
            json={
                "project": project_name,
                "entries": {
                    "student": stud,
                    "test": subj,
                    "score": sc,
                    "timestamp": ts,
                },
            },
            headers=HEADERS,
        )
        assert resp.status_code == 200, resp.text

    # We'll do group_by=["entries/student"], with:
    #   - "sort_type='sort_groups'" on the 'score' aggregator => mean => descending
    #   - "sorting" for "timestamp" => ascending => applies within groups
    group_sorting = {
        "entries/student": {
            "field": "score",
            "direction": "descending",
            "metric": "mean",
        },
    }
    # Meanwhile "sorting" is just standard JSON for "timestamp" => ascending
    # e.g. sorting='{"timestamp":"ascending"}'
    sorting_within = {"timestamp": "ascending"}

    resp = await client.get(
        "/v0/logs",
        params={
            "project": project_name,
            "group_by": ["entries/student"],
            "group_sorting": json.dumps(group_sorting),
            "sorting": json.dumps(sorting_within),
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200
    result = resp.json()

    # The grouped data is in result["logs"]["entries/student"]
    logs_section = result.get("logs", {})
    assert "entries/student" in logs_section, "Expected top-level grouping by 'student'"
    student_obj = logs_section["entries/student"]

    # Let's gather all top-level group keys (the student names)
    # ignoring "group_count", "count", or aggregator keys
    group_keys = [
        k
        for k in student_obj.keys()
        if k not in ("group_count", "count", "_aggregator_metric")
    ]

    # 1) Check the across-groups order => mean(score) descending
    from statistics import mean

    # We'll build a map from student->(scores, mean_score)
    stud_scores_map = {}
    for (stud, subj, sc, ts) in data:
        stud_scores_map.setdefault(stud, []).append(sc)
    means_map = {st: mean(vals) for st, vals in stud_scores_map.items()}

    # The order in the JSON is group_keys
    for i in range(len(group_keys) - 1):
        cur_st = group_keys[i]
        nxt_st = group_keys[i + 1]
        # each should follow means_map[cur_st] >= means_map[nxt_st]
        assert means_map[cur_st] >= means_map[nxt_st], (
            f"Groups not sorted by descending mean(score). "
            f"Student {cur_st} has {means_map[cur_st]}, next student {nxt_st} has {means_map[nxt_st]}"
        )

    # 2) Check "within-groups" ordering => we said sorting={"timestamp":"ascending"}
    # so each student's logs must appear from earliest->latest.
    for st in group_keys:
        if st in ("group_count", "count", "_aggregator_metric"):
            continue
        grp_val = student_obj[st]
        # if it's a list, we read it directly
        if isinstance(grp_val, list):
            logs_list = grp_val
        elif isinstance(grp_val, dict) and "_leaf_logs" in grp_val:
            logs_list = grp_val["_leaf_logs"]
        else:
            # Single-level grouping might produce a plain list or a dictionary with extra fields
            raise AssertionError(
                f"Unexpected shape for grouping of student={st}: {grp_val}",
            )

        actual_ts_list = []
        for log_item in logs_list:
            actual_ts_list.append(log_item["entries"]["timestamp"])

        # check ascending
        for i in range(len(actual_ts_list) - 1):
            assert actual_ts_list[i] <= actual_ts_list[i + 1], (
                f"Logs not in ascending timestamp within group {st}. "
                f"{actual_ts_list[i]} vs {actual_ts_list[i+1]}"
            )


@pytest.mark.anyio
@pytest.mark.skip(reason="Skipping test due to response structure changes")
async def test_get_logs_groupby_with_other_filters(client: AsyncClient):
    project_name = "test-grouping-with-other-filters"
    _ = await _create_project(client, project_name)

    # Create the standard logs you used before:
    await _create_several_logs(client, project_name)

    # Create derived logs for testing grouping
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

    # Create a few extra logs that have param "a/b/param2" and other fields:
    custom_logs_for_param_versions = [
        {
            "params": {"a/b/param1": "extra_test_1", "a/b/param2": "0"},
            "entries": {
                "_/description": "param-version log #1",
                "_/state": "extra_liquid",
                "_/safe": True,
            },
        },
        {
            "params": {"a/b/param1": "extra_test_2", "a/b/param2": "1"},
            "entries": {
                "_/description": "param-version log #2",
                "_/state": "extra_liquid",
                "_/safe": False,
            },
        },
        {
            "params": {"a/b/param1": "extra_test_3", "a/b/param2": "1"},
            "entries": {
                "_/description": "param-version log #3",
                "_/state": "extra_vapor",
                "_/safe": True,
            },
        },
    ]
    for item in custom_logs_for_param_versions:
        response = await client.post(
            "/v0/logs",
            json={
                "project": project_name,
                "params": item["params"],
                "entries": item["entries"],
            },
            headers=HEADERS,
        )
        assert response.status_code == 200, response.json()

    #
    # ==========  SCENARIO A: group_by + from_fields  ==========
    #
    # group by "entries/_/state" but only include logs that have either
    # "entries/_/state" or "entries/_/description" (from_fields).
    # This should exclude logs that lack these keys entirely.
    response = await client.get(
        "/v0/logs",
        params={
            "project": project_name,
            "group_by": ["entries/_/state"],
            "from_fields": "_/description&_/state",  # only logs w/ these keys
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    result = response.json()

    logs_section = result["logs"]
    assert "entries/_/state" in logs_section
    group_obj = logs_section["entries/_/state"]
    assert "group_count" in group_obj
    assert "count" in group_obj

    # All logs that do NOT have _/state or _/description will be filtered out entirely,
    assert (
        group_obj["count"] == 9
    ), f"Expected 10 logs that contain either _/description or _/state, got {group_obj['count']}"

    # Verify each returned log only has the from_fields in "entries":
    for group_name, logs_or_meta in group_obj.items():
        if group_name in ("group_count", "count"):
            continue
        assert isinstance(logs_or_meta, list)
        for log in logs_or_meta:
            for field in log["entries"].keys():
                # Should only see _/description
                assert field in ("_/description",), f"Unexpected field: {field}"

    #
    # ==========  SCENARIO B: group_by + exclude_fields  ==========
    #
    # Exclude `_/description`, and group by `_/state`. We expect the logs to be grouped
    # by `_/state`, but none of the returned logs should contain `_/description`.
    response = await client.get(
        "/v0/logs",
        params={
            "project": project_name,
            "group_by": ["entries/_/state"],
            "exclude_fields": "_/description",  # remove the description field
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    result = response.json()

    logs_section = result["logs"]
    assert "entries/_/state" in logs_section
    group_obj = logs_section["entries/_/state"]
    assert "group_count" in group_obj
    assert "count" in group_obj

    # Check logs to ensure `_/description` is excluded from each "entries" dict
    for group_name, logs_or_meta in group_obj.items():
        if group_name in ("count", "group_count"):
            continue
        for log in logs_or_meta:
            assert "_/description" not in log["entries"]

    #
    # ==========  SCENARIO C: group_by + from_ids (or exclude_ids)  ==========
    #
    # Pick a small subset of log_event_ids: for instance, the first 2 logs + the
    # "param-version log #1"
    # Group by "params/a/b/param1" now, but it should only return logs
    # that match these IDs.
    from_ids_example = "1&2&8"
    response = await client.get(
        "/v0/logs",
        params={
            "project": project_name,
            "group_by": ["params/a/b/param1"],
            "from_ids": from_ids_example,
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    result = response.json()
    logs_section = result["logs"]
    assert "params/a/b/param1" in logs_section

    param1_section = logs_section["params/a/b/param1"]
    assert "count" in param1_section
    # We expect exactly the logs with event_ids = 1, 2, 8
    # That should be 3 total logs if all exist with those IDs
    assert (
        param1_section["count"] == 3
    ), f"Expected 3 logs, got {param1_section['count']}"

    # We can also verify that no other logs appear:
    for k, subval in param1_section.items():
        if k in ("group_count", "count"):
            continue
        # subval should be a list of logs
        for log in subval:
            assert log["id"] in (1, 2, 8), f"Found unexpected log ID: {log['id']}"

    #
    # ==========  SCENARIO D: group_by + filter_expr  ==========
    #
    # Filter by temperature above 100.
    # Then group by `_/state` for demonstration.
    response = await client.get(
        "/v0/logs",
        params={
            "project": project_name,
            "group_by": ["entries/_/state"],
            "filter_expr": "_/temperature > 0",
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    result = response.json()

    logs_section = result["logs"]
    assert "entries/_/state" in logs_section
    group_obj = logs_section["entries/_/state"]
    assert "count" in group_obj
    assert "group_count" in group_obj
    # Confirm that each returned log has a temperature above 100
    for group_name, logs_or_meta in group_obj.items():
        if group_name in ("count", "group_count"):
            continue
        for log in logs_or_meta:
            temp = log["entries"].get("_/temperature")
            if isinstance(temp, str):
                temp_float = float(temp)
            else:
                temp_float = temp
            assert temp_float > 0, f"Expected temp>0, found {temp_float}"

    #
    # ==========  SCENARIO E: group_by + sorting + limit/offset at the leaf level  ==========
    #
    # Group by `entries/_/state`, then inside each state's group we want to
    # sort by `_/description` ascending, but only return the first 1 log (limit=1)
    # (and skip 0 logs offset=0). This ensures we see that each group's list is
    # truncated by limit=1 and sorted by description.
    response = await client.get(
        "/v0/logs",
        params={
            "project": project_name,
            "group_by": ["entries/_/state"],
            "sorting": '{"_/description":"descending"}',
            "limit": 1,
            "offset": 0,
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    result = response.json()

    logs_section = result["logs"]
    assert "entries/_/state" in logs_section
    group_obj = logs_section["entries/_/state"]
    assert "count" in group_obj
    assert "group_count" in group_obj
    # Now each group is a list of *1* log, sorted by description.

    for state_val, logs_or_meta in group_obj.items():
        if state_val in ("count", "group_count"):
            continue
        assert (
            len(logs_or_meta) <= 1
        ), f"Expected limit=1 log per group, got {len(logs_or_meta)}"
        if len(logs_or_meta) == 1:
            single_log = logs_or_meta[0]
            # Check presence of fields
            assert "id" in single_log and "ts" in single_log
            assert "entries" in single_log and "params" in single_log

    # Test sorting by a single group field
    response = await client.get(
        "/v0/logs",
        params={
            "project": project_name,
            "group_by": ["entries/_/state"],
            "sorting": json.dumps({"_/state": "ascending"}),
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    result = response.json()

    # Get the state groups in order they appear
    logs_section = result["logs"]
    assert "entries/_/state" in logs_section
    group_obj = logs_section["entries/_/state"]

    # Get all group names excluding metadata keys
    group_names = [k for k in group_obj.keys() if k not in ("group_count", "count")]

    # Verify ascending order of state groups
    # Note: null should be at the end in ascending order
    non_null_groups = [g for g in group_names if g != "null"]
    assert (
        sorted(non_null_groups) == non_null_groups
    ), "Groups should be in ascending order"
    assert "null" in group_names, "Null group should be present"
    assert group_names[-1] == "null", "Null group should be last in ascending order"

    # Test sorting by multiple group fields
    # This would be relevant when we have multiple group-by fields
    response = await client.get(
        "/v0/logs",
        params={
            "project": project_name,
            "group_by": ["entries/_/state", "entries/_/safe"],
            "sorting": json.dumps(
                {
                    "_/state": "ascending",
                    "_/safe": "descending",
                },
            ),
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    result = response.json()

    logs_section = result["logs"]
    assert "entries/_/state" in logs_section
    group_obj = logs_section["entries/_/state"]

    # Verify the outer groups (states) are in ascending order
    state_groups = [k for k in group_obj.keys() if k not in ("group_count", "count")]
    non_null_states = [g for g in state_groups if g != "null"]
    assert (
        sorted(non_null_states) == non_null_states
    ), "State groups should be in ascending order"

    # For each state group, verify the inner groups (safe values) are in descending order
    for state in non_null_states:
        if isinstance(group_obj[state], dict) and "entries/_/safe" in group_obj[state]:
            safe_groups = group_obj[state]["entries/_/safe"]
            safe_values = [
                k for k in safe_groups.keys() if k not in ("group_count", "count")
            ]
            non_null_safes = [s for s in safe_values if s != "null"]
            assert (
                sorted(non_null_safes, reverse=True) == non_null_safes
            ), f"Safe groups in state={state} should be in descending order"

    #
    # ==========  SCENARIO F: Group by Derived Log Fields  ==========
    #
    # Test grouping by derived log 'derived_temp' which is defined as temperature + 10
    response = await client.get(
        "/v0/logs",
        params={
            "project": project_name,
            "group_by": ["derived_entries/derived_temp"],
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    result = response.json()
    logs_section = result["logs"]
    assert "derived_entries/derived_temp" in logs_section
    group_obj = logs_section["derived_entries/derived_temp"]
    assert "count" in group_obj
    assert "group_count" in group_obj

    # Verify each group's derived value matches temperature + 10
    for derived_val_str, logs_list in group_obj.items():
        if derived_val_str in ("count", "group_count", "null"):
            continue
        derived_val = float(derived_val_str)
        for log in logs_list:
            orig_temp = log["entries"].get("_/temperature")
            if orig_temp is not None:
                assert (
                    derived_val == orig_temp + 10
                ), f"Derived temp mismatch in log {log['id']}"

    # Test grouping by derived log 'state_len' which computes length of state field
    response = await client.get(
        "/v0/logs",
        params={
            "project": project_name,
            "group_by": ["derived_entries/state_len"],
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    result = response.json()
    logs_section = result["logs"]
    assert "derived_entries/state_len" in logs_section
    group_obj = logs_section["derived_entries/state_len"]
    assert "count" in group_obj
    assert "group_count" in group_obj

    # Verify each group's derived value matches state length
    for state_len_str, logs_list in group_obj.items():
        if state_len_str in ("count", "group_count", "null"):
            continue
        state_len = float(state_len_str)
        for log in logs_list:
            state = log["entries"].get("_/state")
            if state is not None:
                assert state_len == len(
                    state,
                ), f"State length mismatch in log {log['id']}"

    # Test multi-level grouping by both derived logs: first by 'derived_temp' then by 'state_len'
    response = await client.get(
        "/v0/logs",
        params={
            "project": project_name,
            "group_by": ["derived_entries/derived_temp", "derived_entries/state_len"],
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    result = response.json()
    logs_section = result["logs"]
    assert "derived_entries/derived_temp" in logs_section
    temp_groups = logs_section["derived_entries/derived_temp"]

    # Verify nested grouping structure and calculations
    for temp_val_str, state_len_groups_wrapper in temp_groups.items():
        if temp_val_str in ("count", "group_count", "null"):
            continue
        assert "derived_entries/state_len" in state_len_groups_wrapper
        len_groups = state_len_groups_wrapper["derived_entries/state_len"]

        for len_val_str, logs_list in len_groups.items():
            if len_val_str in ("count", "group_count"):
                continue
            for log in logs_list:
                orig_temp = log["entries"].get("_/temperature")
                state = log["entries"].get("_/state")
                if orig_temp is not None:
                    assert (
                        float(temp_val_str) == orig_temp + 10
                    ), f"Derived temp mismatch in multi-level grouping for log {log['id']}"
                if state is not None:
                    assert float(len_val_str) == len(
                        state,
                    ), f"Derived state length mismatch in multi-level grouping for log {log['id']}"


@pytest.mark.anyio
@pytest.mark.skip(reason="Skipping test due to response structure changes")
async def test_get_logs_multi_level_nested_and_flat(client: AsyncClient):
    project_name = "test-multi-level-grouping"
    await _create_project(client, project_name)

    for i in [0, 1]:
        for j in [0, 1, 2, 3]:
            payload = {
                "project": project_name,
                "params": {"sys_msg": "hello"},
                "entries": {"i": i, "j": j},
            }
            response = await client.post("/v0/logs", json=payload, headers=HEADERS)
            assert response.status_code == 200, response.json()

    # Test nested grouping (nested_groups=True)
    params_nested = {
        "project": project_name,
        "group_by": ["params/sys_msg", "entries/i", "entries/j"],
        "nested_groups": True,
    }
    response_nested = await client.get(
        "/v0/logs",
        params=params_nested,
        headers=HEADERS,
    )
    assert response_nested.status_code == 200
    result_nested = response_nested.json()

    assert "params" in result_nested
    assert result_nested["params"].get("sys_msg", {}).get("0") == "hello"

    assert "logs" in result_nested
    logs_nested = result_nested["logs"]
    assert "params/sys_msg" in logs_nested
    group_sys_msg = logs_nested["params/sys_msg"]
    assert "0" in group_sys_msg

    group_i = group_sys_msg["0"]
    assert "entries/i" in group_i
    group_i_data = group_i["entries/i"]
    keys_i = [k for k in group_i_data.keys() if k not in ("group_count", "count")]
    assert set(keys_i) == {"0", "1"}

    for i_key in keys_i:
        group_j_wrapper = group_i_data[i_key]
        assert "entries/j" in group_j_wrapper
        group_j = group_j_wrapper["entries/j"]
        keys_j = [k for k in group_j.keys() if k not in ("group_count", "count")]
        assert set(keys_j) == {"0", "1", "2", "3"}
        for j_key in keys_j:
            leaf = group_j[j_key]
            assert isinstance(leaf, list)

    # Test flat grouping (nested_groups=False)
    params_flat = {
        "project": project_name,
        "group_by": ["params/sys_msg", "entries/i", "entries/j"],
        "nested_groups": False,
    }
    response_flat = await client.get("/v0/logs", params=params_flat, headers=HEADERS)
    assert response_flat.status_code == 200
    result_flat = response_flat.json()

    assert "groups" in result_flat
    groups = result_flat["groups"]

    for key in ["params/sys_msg", "entries/i", "entries/j"]:
        assert key in groups

    group_sys_msg_flat = groups["params/sys_msg"]
    assert "0" in group_sys_msg_flat

    group_i_flat = groups["entries/i"]
    keys_i_flat = [k for k in group_i_flat.keys() if k not in ("group_count", "count")]
    assert set(keys_i_flat) == {"0", "1"}

    for i_key in keys_i_flat:
        ids = group_i_flat[i_key]
        assert all(isinstance(_id, int) for _id in ids)

    flat_logs = result_flat["logs"]
    assert isinstance(flat_logs, list)
    assert len(flat_logs) <= 8
    assert result_flat.get("count") == 8


@pytest.mark.anyio
@pytest.mark.skip(reason="Skipping test due to response structure changes")
async def test_get_logs_groups_only_and_return_timestamps(client: AsyncClient):
    project_name = "test-groups-only"
    await _create_project(client, project_name)

    # Create 8 logs: for i in [0,1] and j in [0,1,2,3]
    for i in [0, 1]:
        for j in [0, 1, 2, 3]:
            payload = {
                "project": project_name,
                "params": {"sys_msg": "hello"},
                "entries": {"i": i, "j": j},
            }
            response = await client.post("/v0/logs", json=payload, headers=HEADERS)
            assert response.status_code == 200, response.json()

    # Quick sanity check: we have 8 logs in normal, non-grouped mode
    response = await client.get(
        "/v0/logs",
        params={"project": project_name},
        headers=HEADERS,
    )
    assert response.status_code == 200
    result = response.json()
    assert "logs" in result
    assert len(result["logs"]) == 8

    # ----------------------------------------------------------------
    # CASE A: Nested groups, groups_only=True, return_timestamps=False
    #   group_by = ["params/sys_msg", "entries/i"]
    #
    # At the final leaf, we have a list of log IDs (no "j" field is visible,
    # because groups_only=True discards full log objects).
    # ----------------------------------------------------------------
    params_nested = {
        "project": project_name,
        "group_by": ["params/sys_msg", "entries/i"],
        "nested_groups": True,
        "groups_only": True,
        "return_timestamps": False,
    }
    response_nested = await client.get(
        "/v0/logs",
        params=params_nested,
        headers=HEADERS,
    )
    assert response_nested.status_code == 200
    result_nested = response_nested.json()
    assert "logs" in result_nested
    logs_nested = result_nested["logs"]

    sys_msg_group = logs_nested.get("params/sys_msg", {})
    assert (
        "0" in sys_msg_group
    ), f"Missing param version 0 group. Got keys: {list(sys_msg_group.keys())}"
    i_group = sys_msg_group["0"].get("entries/i", {})
    # i_group should have keys "0" and "1", each pointing to the final leaf (a list of IDs).
    for i_key in ["0", "1"]:
        leaf = i_group.get(i_key)
        assert leaf is not None, f"Missing sub-group for i={i_key}"
        # Because we've run out of group_by fields, 'leaf' should be a list of IDs:
        assert isinstance(leaf, list), f"Leaf for i={i_key} is not a list of IDs"
        # Each item in that list should be an integer log ID.
        for log_id in leaf:
            assert isinstance(log_id, int), f"Expected int log_id, got {type(log_id)}"

    # ----------------------------------------------------------------
    # CASE B: Nested groups, groups_only=True, return_timestamps=True
    #   group_by = ["params/sys_msg", "entries/i"]
    #
    # The final leaves become dicts of { log_id: "YYYY-MM-DDTHH:MM:SS" }.
    # ----------------------------------------------------------------
    params_nested_ts = {
        "project": project_name,
        "group_by": ["params/sys_msg", "entries/i"],
        "nested_groups": True,
        "groups_only": True,
        "return_timestamps": True,
    }
    response_nested_ts = await client.get(
        "/v0/logs",
        params=params_nested_ts,
        headers=HEADERS,
    )
    assert response_nested_ts.status_code == 200
    result_nested_ts = response_nested_ts.json()

    # Similar structure as Case A, but final leaves are a dict of {id:timestamp}.
    logs_nested_ts = result_nested_ts["logs"]
    sys_msg_group_ts = logs_nested_ts.get("params/sys_msg", {})
    assert "0" in sys_msg_group_ts
    i_group_ts = sys_msg_group_ts["0"].get("entries/i", {})

    for i_key in ["0", "1"]:
        leaf_ts = i_group_ts.get(i_key)
        # Now the leaf should be a dict:
        assert isinstance(
            leaf_ts,
            dict,
        ), f"Expected a dict of {{log_id: timestamp}} at i={i_key}, got {type(leaf_ts)}"
        for log_id_str, timestamp in leaf_ts.items():
            # log_id_str is a string key, parse it to int to confirm
            log_id_int = int(log_id_str)  # will raise ValueError if not valid
            assert isinstance(
                timestamp,
                str,
            ), f"Expected a timestamp string, got {type(timestamp)}"

    # ----------------------------------------------------------------
    # CASE C: Flat groups, groups_only=True, return_timestamps=False
    #   group_by = ["params/sys_msg", "entries/i"]
    #
    # We do NOT nest the groups. Instead, we get "groups": {
    #    "params/sys_msg": { "0": [...IDs...], "group_count":1, "count":8 },
    #    "entries/i":      { "0": [...IDs...], "1": [...IDs...], "group_count":2, "count":8 }
    # }
    # and no "logs" key is returned (since groups_only=True).
    # ----------------------------------------------------------------
    params_flat = {
        "project": project_name,
        "group_by": ["params/sys_msg", "entries/i"],
        "nested_groups": False,
        "groups_only": True,
        "return_timestamps": False,
    }
    response_flat = await client.get("/v0/logs", params=params_flat, headers=HEADERS)
    assert response_flat.status_code == 200
    result_flat = response_flat.json()

    # Because nested_groups=False + groups_only=True, the response has "groups" but no "logs".
    assert "groups" in result_flat
    assert "logs" not in result_flat
    groups = result_flat["groups"]

    # We expect two top-level group entries: "params/sys_msg" and "entries/i".
    assert "params/sys_msg" in groups
    assert "entries/i" in groups

    # For "params/sys_msg", there's only one distinct version => "0".
    sys_msg_flat = groups["params/sys_msg"]
    # "group_count"=1, "count"=8, plus a key "0": [...list of 8 log IDs...]
    assert "0" in sys_msg_flat
    assert isinstance(sys_msg_flat["0"], list)
    assert len(sys_msg_flat["0"]) == 8, "All logs share the same sys_msg=hello"

    # For "entries/i", we have i=0 or i=1. Each should have 4 logs.
    i_flat = groups["entries/i"]
    for i_key in ("0", "1"):
        assert i_key in i_flat
        assert isinstance(i_flat[i_key], list)
        assert len(i_flat[i_key]) == 4, f"Expected 4 logs with i={i_key}"
        for log_id in i_flat[i_key]:
            assert isinstance(log_id, int)
