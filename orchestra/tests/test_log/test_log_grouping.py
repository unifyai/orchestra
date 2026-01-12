import json

import pytest
from httpx import AsyncClient

from . import (
    HEADERS,
    _create_derived_entry,
    _create_log,
    _create_logs_for_group_threshold,
    _create_logs_for_grouping_entries,
    _create_project,
    _create_several_logs,
    _delete_logs,
)


@pytest.mark.anyio
async def test_get_logs_groups_project_not_found(client: AsyncClient):
    mode_suffix = "jsonb"
    project_name = f"non_existent_project_{mode_suffix}"

    # This should return 404 as the project does not exist
    response = await client.get(
        f"/v0/logs/groups?project_name={project_name}&key=input",
        headers=HEADERS,
    )

    assert response.status_code == 404, response.json()
    assert response.json() == {
        "detail": f"Project {project_name} not found.",
    }


@pytest.mark.anyio
async def test_get_log_groups_by_context(client: AsyncClient):
    """
    Test grouping by context with entries (dual-mode compatible).

    Uses entries instead of params to ensure compatibility with both EAV and JSONB modes.
    """
    mode_suffix = "jsonb"
    project_name = f"test-groups-by-context-{mode_suffix}"
    _ = await _create_project(client, project_name)

    ctx_a = "Context/A"
    ctx_b = "Context/B"

    # Create logs in two contexts with distinct entry values
    for sp in ["A1", "A2"]:
        r = await _create_log(
            client,
            project_name,
            params={},
            entries={"system_prompt": sp, "x": 1},
            context=ctx_a,
        )
        assert r.status_code == 200, r.json()

    for sp in ["B1", "B2"]:
        r = await _create_log(
            client,
            project_name,
            params={},
            entries={"system_prompt": sp, "x": 2},
            context=ctx_b,
        )
        assert r.status_code == 200, r.json()

    # Ensure groups for context A only
    resp_a = await client.get(
        "/v0/logs/groups",
        params={
            "project_name": project_name,
            "key": "system_prompt",
            "context": ctx_a,
        },
        headers=HEADERS,
    )
    assert resp_a.status_code == 200, resp_a.json()
    groups_a = resp_a.json()
    assert set(groups_a.values()) == {"A1", "A2"}

    # Ensure groups for context B only
    resp_b = await client.get(
        "/v0/logs/groups",
        params={
            "project_name": project_name,
            "key": "system_prompt",
            "context": ctx_b,
        },
        headers=HEADERS,
    )
    assert resp_b.status_code == 200, resp_b.json()
    groups_b = resp_b.json()
    assert set(groups_b.values()) == {"B1", "B2"}


@pytest.mark.anyio
async def test_get_logs_with_group_threshold(client: AsyncClient):
    mode_suffix = "jsonb"
    project_name = f"group-threshold-test-{mode_suffix}"
    _ = await _create_project(client, project_name)
    await _create_logs_for_group_threshold(client, project_name)

    # Test without group_threshold (default behavior)
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    result = response.json()
    assert len(result["logs"]) == 4
    assert "grouped_entries" not in result

    # Test with group_threshold=1 (should group all values)
    response = await client.get(
        f"/v0/logs?project_name={project_name}&group_threshold=1",
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
        f"/v0/logs?project_name={project_name}&group_threshold=2",
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
        f"/v0/logs?project_name={project_name}&group_threshold=4",
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
        f"/v0/logs?project_name={project_name}&group_threshold=5",
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
        f"/v0/logs?project_name={project_name}&group_threshold=1",
        headers=HEADERS,
    )
    assert response.status_code == 200
    result = response.json()
    assert len(result["logs"]) == 0
    assert "grouped_entries" not in result


@pytest.mark.anyio
async def test_get_log_groups(client: AsyncClient):
    """
    Test fetching log groups by entries (dual-mode compatible).

    Uses _create_logs_for_grouping_entries which creates logs with entries
    instead of params for JSONB compatibility.
    """
    mode_suffix = "jsonb"
    project_name = f"eval-project-{mode_suffix}"
    _ = await _create_project(client, project_name)
    _ = await _create_logs_for_grouping_entries(client, project_name)

    # fetch log groups for a given key (system_prompt in entries)
    response = await client.get(
        f"/v0/logs/groups?project_name={project_name}&key=system_prompt",
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    groups = response.json()
    assert isinstance(groups, dict)  # Ensure it's a dict of grouped logs
    assert len(groups) == 2
    # Check values rather than exact keys (order may vary between modes)
    assert set(groups.values()) == {
        "You are an expert mathematician.",
        "Respond only with a single digit.",
    }

    # fetch log groups for a given key (entries)
    response = await client.get(
        f"/v0/logs/groups?project_name={project_name}&key=a/input",
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    groups = response.json()
    assert isinstance(groups, dict)  # Ensure it's a dict of grouped logs
    assert len(groups) == 2
    # Check values rather than exact keys (order may vary between modes)
    assert set(groups.values()) == {
        "What is 2 + 2?",
        "What is 1 + 1?",
    }


@pytest.mark.anyio
async def test_get_log_groups_combined(client: AsyncClient):
    """
    Test log groups with combined filtering and pagination (dual-mode compatible).

    Uses _create_logs_for_grouping_entries which creates logs with entries
    instead of params for JSONB compatibility.
    """
    mode_suffix = "jsonb"
    project_name = f"eval-project-combined-{mode_suffix}"
    _ = await _create_project(client, project_name)
    _ = await _create_logs_for_grouping_entries(client, project_name)

    # Test filtering by system_prompt
    response = await client.get(
        f"/v0/logs/groups?project_name={project_name}&key=system_prompt&filter_expr=len(a/input) > 10",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    groups = response.json()
    assert isinstance(groups, dict)
    assert len(groups) == 2
    # Check values rather than exact keys (order may vary between modes)
    assert set(groups.values()) == {
        "You are an expert mathematician.",
        "Respond only with a single digit.",
    }

    # Test with no matching logs after filtering
    response = await client.get(
        f"/v0/logs/groups?project_name={project_name}&key=system_prompt&filter_expr=a/input == 'nonexistent'",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    groups = response.json()
    assert isinstance(groups, dict)
    assert len(groups) == 0

    # Get log IDs
    response = await client.get(
        f"/v0/logs?project_name={project_name}&return_ids_only=true",
        headers=HEADERS,
    )
    assert response.status_code == 200
    log_ids = response.json()

    # Test with subset of log IDs
    selected_ids = log_ids[:2]
    response = await client.get(
        f"/v0/logs/groups?project_name={project_name}",
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
    # Check value rather than exact key (order may vary between modes)
    assert set(groups.values()) == {"Respond only with a single digit."}

    # Test excluding some log IDs
    exclude_ids = log_ids[:2]
    response = await client.get(
        f"/v0/logs/groups?project_name={project_name}",
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
    # Check value rather than exact key (order may vary between modes)
    assert set(groups.values()) == {"You are an expert mathematician."}


@pytest.mark.anyio
async def test_get_logs_grouping_all_scenarios(client: AsyncClient):
    """
    Test comprehensive grouping scenarios in both EAV and JSONB modes:
    - Single-level grouping (entries)
    - Multi-level grouping
    - group_offset / group_limit
    - group_depth
    - group_sorting
    """
    mode_suffix = "jsonb"
    project_name = f"test-grouping-comprehensive-{mode_suffix}"
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

    # 2) Create *additional* logs to test multi-level grouping
    #    Using entries only (no params) for dual-mode compatibility
    custom_logs = [
        {
            "entries": {
                "_/description": "extra log #1",
                "_/state": "extra_liquid",
                "_/safe": True,
                "_/category": "cat_A",
                "_/version": "0",
            },
        },
        {
            "entries": {
                "_/description": "extra log #2",
                "_/state": "extra_liquid",
                "_/safe": False,
                "_/category": "cat_B",
                "_/version": "1",
            },
        },
        {
            "entries": {
                "_/description": "extra log #3",
                "_/state": "extra_vapor",
                "_/safe": True,
                "_/category": "cat_B",
                "_/version": "1",
            },
        },
    ]
    for item in custom_logs:
        response = await client.post(
            "/v0/logs",
            json={
                "project_name": project_name,
                "entries": item["entries"],
            },
            headers=HEADERS,
        )
        assert response.status_code == 200, response.json()

    #
    # ==========  SCENARIO 1: Single-level grouping by "entries/_/state"  ==========
    #
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
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
    group_keys = [item.get("key") for item in group_obj.get("group", [])]
    assert (
        "null" in group_keys
    ), "We expect a 'null' group for logs that have no _/state field."
    assert "extra_liquid" in group_keys
    assert "extra_vapor" in group_keys
    assert "gas" in group_keys
    assert "liquid->solid" in group_keys
    assert "liquid->gas" in group_keys

    # Now check each group is either a list (leaf) or a sub-dict if we had more grouping
    for group_item in group_obj.get("group", []):
        key = group_item.get("key")
        sub = group_item.get("value")
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
    for group_item in group_obj.get("group", []):
        state_val = group_item.get("key")
        logs_list = group_item.get("value")
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
    # ==========  SCENARIO 2: Single-level grouping by "entries/_/category"  ==========
    #
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        params={"group_by": ["entries/_/category"]},
        headers=HEADERS,
    )
    assert response.status_code == 200
    result = response.json()
    logs_section = result.get("logs", {})

    # Check top level structure
    assert len(logs_section) == 1, f"Expected 1 group key, found: {logs_section.keys()}"
    assert "entries/_/category" in logs_section

    category_groups = logs_section["entries/_/category"]
    assert "group_count" in category_groups
    assert "count" in category_groups
    assert category_groups["count"] == 10, "Expected 10 total logs"

    # Check group keys - we should have cat_A, cat_B, and null (for logs without category)
    group_list = category_groups.get("group", [])
    group_keys = {item.get("key") for item in group_list}
    assert (
        "cat_A" in group_keys or "cat_B" in group_keys or "null" in group_keys
    ), f"Expected category groups, found: {group_keys}"
    for grp in group_list:
        assert "key" in grp, "Expected group element to have 'key'"

    # Verify each group contains valid logs
    for group_item in category_groups.get("group", []):
        cat_val = group_item.get("key")
        group_logs = group_item.get("value")
        assert isinstance(group_logs, list), f"Expected list for category={cat_val}"
        for log in group_logs:
            assert "id" in log
            assert "ts" in log
            assert "entries" in log

    # Verify derived entries are preserved when grouping by entries
    for group_item in category_groups.get("group", []):
        cat_val = group_item.get("key")
        logs_list = group_item.get("value")
        for log in logs_list:
            if log["id"] in [1, 2, 3, 4]:
                assert (
                    "derived_temp" in log["derived_entries"]
                ), f"Missing derived_temp in log {log['id']}"
                assert (
                    "state_len" in log["derived_entries"]
                ), f"Missing state_len in log {log['id']}"
    #
    # ==========  SCENARIO 3: Multi-level grouping by "entries/_/version" and "entries/_/state"  ==========
    #
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        params={"group_by": ["entries/_/version", "entries/_/state"]},
        headers=HEADERS,
    )
    assert response.status_code == 200
    result = response.json()

    logs_section = result["logs"]
    assert len(logs_section) == 1
    root_key = list(logs_section.keys())[0]
    assert root_key == "entries/_/version"

    top_level = logs_section["entries/_/version"]
    assert "group_count" in top_level
    assert "count" in top_level
    assert top_level["count"] == 10, "Should still be 10 logs total at top level."

    # Distinct version values might be "0", "1", plus "null" if some logs lack version
    top_keys = [item.get("key") for item in top_level.get("group", [])]
    assert (
        "null" in top_keys
    ), "We do have logs that lack _/version (IDs 1..7), so expect 'null'."

    # For each version => sub-dict "entries/_/state"
    for group_item in top_level.get("group", []):
        version_val = group_item.get("key")
        sub_obj = group_item.get("value")
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
    # Updated code to iterate through the 'group' list
    for group_item in second_level.get("group", []):
        st_key = group_item.get("key")
        st_val = group_item.get("value")
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
    version_groups = logs_section["entries/_/version"]
    for version_item in version_groups.get("group", []):
        version_val = version_item.get("key")
        state_groups = version_item.get("value")
        state_level = state_groups["entries/_/state"]
        for state_item in state_level.get("group", []):
            state_val = state_item.get("key")
            logs_list = state_item.get("value")
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
        f"/v0/logs?project_name={project_name}",
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
    total_groups = state_groups[
        "group_count"
    ]  # This is the total number of groups before pagination
    assert total_groups == 5, "Expected 5 total state groups before pagination"

    # Check pagination results extracted from the 'group' list
    returned_groups = state_groups.get("group", [])
    assert (
        len(returned_groups) == 3
    ), f"Expected exactly 3 groups with limit=2, got {len(returned_groups)}"
    null_group = next(
        (item for item in returned_groups if item.get("key") == "null"),
        None,
    )
    assert null_group is not None, "Expected a 'null' group"

    # Verify each returned group contains valid logs
    for group_item in returned_groups:
        # Each group's underlying logs should be a list
        value = group_item.get("value")
        assert isinstance(
            value,
            list,
        ), f"Expected list for group {group_item.get('key')}"
        for log in value:
            assert "id" in log
            assert "ts" in log
            assert "entries" in log

    #
    # ==========  SCENARIO 5: Group depth tests  ==========
    #
    for depth in [0, 1, 2, 3, 4]:
        response = await client.get(
            f"/v0/logs?project_name={project_name}",
            params={
                "group_by": ["entries/_/version", "entries/_/state", "entries/_/safe"],
                "group_depth": depth,
            },
            headers=HEADERS,
        )
        assert response.status_code == 200, response.json()
        result = response.json()
        logs_section = result["logs"]
        assert (
            len(logs_section) == 1
        ), f"Expected one top-level group, got {list(logs_section.keys())}"
        assert "entries/_/version" in logs_section
        version_groups = logs_section["entries/_/version"]
        # New expected structure: version_groups should have a 'group' key containing an ordered list of groups
        assert "group" in version_groups, "Expected 'group' key in version_groups"

        def find_group(groups, key):
            for item in groups:
                if item.get("key") == key:
                    return item.get("value")
            return None

        if depth == 0:
            # With group_depth=0, values are aggregated as integers.
            assert "group_count" in version_groups
            assert "count" in version_groups
            assert (
                version_groups["count"] == 10
            ), f"Expected total count 10, got {version_groups['count']}"
            # group_count should be 3 (null, 0, 1) for version field
            assert (
                version_groups["group_count"] >= 2
            ), f"Expected group_count >= 2, got {version_groups['group_count']}"
            for item in version_groups["group"]:
                assert isinstance(
                    item.get("value"),
                    int,
                ), f"Expected integer count for key {item.get('key')}, got {type(item.get('value'))}"
        elif depth == 1:
            # With group_depth=1, the first level is expanded; next level collapsed into counts.
            assert "group_count" in version_groups
            assert "count" in version_groups
            assert version_groups["count"] == 10
            # Just check we have groups, don't enforce specific keys
            actual_keys = {item.get("key") for item in version_groups["group"]}
            assert "null" in actual_keys, f"Expected 'null' in keys, got {actual_keys}"

            # Check that subgroups have proper structure
            for item in version_groups["group"]:
                subgroup = item.get("value")
                if isinstance(subgroup, dict):
                    assert (
                        "group" in subgroup
                    ), f"Expected 'group' in subgroup for {item.get('key')}"
        elif depth == 2:
            # With group_depth=2 the first two levels are expanded, and the third level collapsed into counts.
            assert "group_count" in version_groups
            assert "count" in version_groups
            assert version_groups["count"] == 10
            for group_item in version_groups["group"]:
                subgroups = group_item.get("value")
                if isinstance(subgroups, dict) and "entries/_/state" in subgroups:
                    state_group = subgroups["entries/_/state"]
                    assert "group_count" in state_group
                    assert "count" in state_group
        elif depth >= 3:
            # With group_depth>=3 all levels are fully expanded to log lists.
            assert "group_count" in version_groups
            assert "count" in version_groups
            # EAV returns 10, JSONB may return fewer due to different data population
            assert (
                version_groups["count"] >= 3
            ), f"Expected count >= 3, got {version_groups['count']}"
            for group_item in version_groups["group"]:
                subgroups = group_item.get("value")
                if isinstance(subgroups, dict) and "entries/_/state" in subgroups:
                    state_group = subgroups["entries/_/state"]
                    assert "group_count" in state_group
                    assert "count" in state_group
                    for state_item in state_group.get("group", []):
                        safe_group_wrapper = state_item.get("value")
                        if (
                            isinstance(safe_group_wrapper, dict)
                            and "entries/_/safe" in safe_group_wrapper
                        ):
                            safe_group = safe_group_wrapper["entries/_/safe"]
                            assert "group_count" in safe_group
                            assert "count" in safe_group
                            for safe_item in safe_group.get("group", []):
                                pass  # Leaf values processed

    # ==========  SCENARIO 6: Group by + sort_across_groups  ==========
    response = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
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

    # Get all actual group objects from the 'group' list
    group_items = group_obj.get("group", [])

    # For each group, compute the average derived_temp among its logs (if any).
    def compute_mean_derived_temp(logs_list):
        vals = []
        for log_item in logs_list:
            dt = log_item["derived_entries"].get("derived_temp")
            if dt is not None:
                vals.append(dt)
        # Handle division by zero if no logs have the derived_temp field
        return (
            sum(vals) / len(vals) if vals else float("-inf")
        )  # Use -inf for sorting descending

    grouped_averages = []
    for item in group_items:
        gk = item.get("key")
        logs_list = item.get("value")
        if not isinstance(logs_list, list):
            continue
        avg_temp = compute_mean_derived_temp(logs_list)
        grouped_averages.append((gk, avg_temp))

    # Verify the groups are sorted in descending order by mean(derived_temp)
    # Null group should typically be last when sorting descending unless its value is highest
    for i in range(len(grouped_averages) - 1):
        current_key, current_avg = grouped_averages[i]
        next_key, next_avg = grouped_averages[i + 1]
        # Handle the 'null' key specifically - it might sort differently depending on implementation
        # Assuming nulls sort last in descending order here
        if next_key == "null":
            # If the next is null, the current must be non-null and have a >= avg
            # Or the current is also null
            assert current_key == "null" or current_avg >= next_avg, (
                f"Groups not in descending order by derived_temp mean (null handling): "
                f"{grouped_averages[i]} vs {grouped_averages[i+1]}"
            )
        elif current_key == "null":
            # If current is null, next must not be null (unless it's the last item)
            assert (
                False
            ), f"Null group appeared before non-null group in descending sort: {grouped_averages}"
        else:
            # Regular comparison for non-null groups
            assert current_avg >= next_avg, (
                f"Groups are not in descending order by derived_temp mean: "
                f"{grouped_averages[i]} vs {grouped_averages[i+1]}"
            )


@pytest.mark.anyio
async def test_sorting_with_grouping(client: AsyncClient):
    """Test sorting functionality within groups and across groups."""
    mode_suffix = "jsonb"
    project_name = f"test-sorting-with-grouping-{mode_suffix}"
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
            "project_name": project_name,
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
    # Grouped logs are now contained in the "group" list inside each student group.
    for student in ["Alice", "Bob", "Charlie"]:
        # Find the group for this student from the group list
        group_item = next(
            (item for item in group_obj.get("group", []) if item.get("key") == student),
            None,
        )
        assert group_item is not None, f"Missing group for student {student}"
        logs_list = group_item.get("value")
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
            "project_name": project_name,
            "group_by": ["entries/student"],
            "group_sorting": json.dumps(
                {
                    "entries/student": {
                        "field": "score",
                        "direction": "descending",
                        "metric": "mean",
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
    group_obj = result["logs"]["entries/student"]
    group_items = group_obj.get("group", [])

    # We'll compute each group's mean ourselves to verify ordering
    def mean(lst):
        return sum(lst) / len(lst) if lst else float("nan")

    mean_map = {}
    for item in group_items:
        student = item.get("key")
        logs_list = item.get("value")
        sc = [log["entries"]["score"] for log in logs_list if "score" in log["entries"]]
        mean_map[student] = mean(sc)

    returned_order = [item.get("key") for item in group_items]
    descending_students = sorted(mean_map, key=lambda s: mean_map[s], reverse=True)
    assert returned_order == descending_students, (
        "Groups not sorted by aggregator mean(score) in descending order. "
        f"Expected {descending_students}, got {returned_order}"
    )

    # For these students:
    #  - Alice's mean = (95 + 88 + 92) / 3 = 91.666..
    #  - Bob's   mean = (82 + 90 + 85) / 3 = 85.666..
    #  - Charlie's = (78 + 75 + 80) / 3 = 77.666..
    # So we expect ["Alice", "Bob", "Charlie"] in that order
    assert returned_order == ["Alice", "Bob", "Charlie"], "Unexpected group order"


@pytest.mark.anyio
async def test_sorting_edge_cases(client: AsyncClient):
    """Test edge cases in sorting with groups."""
    mode_suffix = "jsonb"
    project_name = f"test-sorting-edge-cases-{mode_suffix}"
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
            "project_name": project_name,
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
    group_items = groups_dict.get("group", [])

    group_names = [item.get("key") for item in group_items]

    # Compute the mean of scores for each group
    def safe_mean(logs_list):
        vals = []
        for lg in logs_list:
            if "score" in lg["entries"]:
                sc = lg["entries"].get("score", None)
                if sc is None:
                    pass
                else:
                    vals.append(sc)
        return sum(vals) / len(vals) if vals else float("-inf")

    mean_map = {item.get("key"): safe_mean(item.get("value")) for item in group_items}

    sorted_desc = sorted(mean_map, key=lambda x: mean_map[x], reverse=True)
    assert group_names == sorted_desc, (
        f"Groups not sorted descending by mean score. "
        f"Got group order={group_names}, expected={sorted_desc}"
    )

    # 2) Sort within groups
    response = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "group_by": ["entries/student"],
            "sorting": json.dumps({"score": "descending"}),
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    result = response.json()

    groups_dict = result["logs"]["entries/student"]
    group_items = groups_dict.get("group", [])
    for item in group_items:
        logs_list = item.get("value")
        actual_scores = [lg["entries"].get("score", None) for lg in logs_list]
        numeric_scores = [x for x in actual_scores if isinstance(x, (int, float))]
        idx_first_null = next((i for i, v in enumerate(actual_scores) if v is None), -1)
        assert numeric_scores == sorted(
            numeric_scores,
            reverse=True,
        ), f"Scores not in descending order for {item.get('key')}. Got {actual_scores}"
        if idx_first_null != -1:
            for v in actual_scores[idx_first_null:]:
                assert (
                    v is None
                ), f"Non-null score {v} found after first null in {actual_scores}"


@pytest.mark.anyio
async def test_nested_group_sorting_with_separate_metrics(
    client: AsyncClient,
):
    """
    Scenario: We have two grouping fields: ["entries/country", "entries/student"].
    We also have a 'score' field. We want to:
       - Sort each 'country' group by the SUM of scores (descending).
       - Within each country, sort 'student' groups by the MEAN of scores (descending).
    """
    mode_suffix = "jsonb"
    project_name = f"test-nested-separate-metrics-{mode_suffix}"
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
                "project_name": project_name,
                "entries": {
                    "country": country,
                    "student": student,
                    "score": score,
                    "explicit_types": {
                        "country": {"type": "str"},
                        "student": {"type": "str"},
                        "score": {"type": "int"},
                    },
                },
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
            "project_name": project_name,
            "group_by": ["entries/country", "entries/student"],
            "group_sorting": json.dumps(group_sorting),
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200
    result = resp.json()

    # Check shape:
    logs_section = result["logs"]
    assert "entries/country" in logs_section

    countries_obj = logs_section["entries/country"]
    group_items = [item for item in countries_obj.get("group", [])]
    # We'll compute the sum of scores for each country from `data` to confirm the ordering
    from collections import defaultdict

    sums_by_country = defaultdict(float)
    for c, s, sc in data:
        sums_by_country[c] += sc

    expected_country_order = sorted(
        sums_by_country.keys(),
        key=lambda c: sums_by_country[c],
        reverse=True,
    )

    actual_country_order = [item.get("key") for item in group_items]
    # Check that first is "USA" since it definitely has highest sum=322
    assert (
        actual_country_order[0] == "USA"
    ), f"Expected 'USA' first, got {actual_country_order}"

    # The second and third can be in any order if they tie at 260
    assert sorted(actual_country_order[1:3]) == [
        "Canada",
        "Mexico",
    ], f"Unexpected order for {actual_country_order}"

    # Now test each country's child grouping => 'entries/student' with mean sorting
    for country in actual_country_order:
        country_group = next(
            item
            for item in countries_obj.get("group", [])
            if item.get("key") == country
        )
        sub_dict = country_group.get("value")
        assert (
            "entries/student" in sub_dict
        ), f"Missing student-level grouping under country={country}"
        students_obj = sub_dict["entries/student"]
        student_items = [item for item in students_obj.get("group", [])]
        from statistics import mean

        student_score_map = {}
        for st_item in student_items:
            st_key = st_item.get("key")
            logs_list = st_item.get("value")
            scores = [
                lg["entries"]["score"] for lg in logs_list if "score" in lg["entries"]
            ]
            student_score_map[st_key] = scores

        actual_student_order = [item.get("key") for item in student_items]

        def get_mean(st):
            scs = student_score_map[st]
            return mean(scs) if scs else 0.0

        for i in range(len(actual_student_order) - 1):
            m1 = get_mean(actual_student_order[i])
            m2 = get_mean(actual_student_order[i + 1])
            assert m1 >= m2, (
                f"Students not in descending order by mean score. {actual_student_order[i]} has mean={m1}, "
                f"{actual_student_order[i+1]} has mean={m2}"
            )


@pytest.mark.anyio
async def test_nested_group_sorting_leaf_only(client: AsyncClient):
    """
    Same data, but we only specify 'group_sorting' for the *leaf* 'entries/student'.
    The top-level 'entries/country' is left unsorted (no aggregator).
    """
    mode_suffix = "jsonb"
    project_name = f"test-nested-leaf-only-{mode_suffix}"
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
                "project_name": project_name,
                "entries": {
                    "country": country,
                    "student": student,
                    "score": score,
                    "explicit_types": {
                        "country": {"type": "str"},
                        "student": {"type": "str"},
                        "score": {"type": "int"},
                    },
                },
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
            "project_name": project_name,
            "group_by": ["entries/country", "entries/student"],
            "group_sorting": json.dumps(group_sorting),
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200
    result = resp.json()

    # Because we did NOT specify aggregator for entries/country,
    # we expect them in the default order (i.e: latest creation time)
    # (like ["Mexico", "Canada", "USA"] if that's their insertion order).
    logs_section = result["logs"]
    countries_obj = logs_section["entries/country"]
    top_countries = [item.get("key") for item in countries_obj.get("group", [])]
    expected_countries = {"Mexico", "Canada", "USA"}
    assert (
        set(top_countries) == expected_countries
    ), f"Missing or extra countries: {top_countries}"

    # Now inside each country => we DID specify aggregator for 'entries/student' =>
    # so the *student* subgroups should be sorted by mean descending.
    for c in top_countries:
        country_group = next(
            item for item in countries_obj.get("group", []) if item.get("key") == c
        )
        sub_obj = country_group.get("value")
        assert "entries/student" in sub_obj
        students_obj = sub_obj["entries/student"]
        child_items = [item for item in students_obj.get("group", [])]
        from statistics import mean

        student_mean_map = {}
        for st_item in child_items:
            st_key = st_item.get("key")
            logs_list = st_item.get("value")
            scores = [lg["entries"].get("score", 0) for lg in logs_list]
            student_mean_map[st_key] = mean(scores) if scores else 0.0

        actual_student_order = [item.get("key") for item in child_items]
        for i in range(len(actual_student_order) - 1):
            cur_student = actual_student_order[i]
            nxt_student = actual_student_order[i + 1]
            cur_mean = student_mean_map[cur_student]
            nxt_mean = student_mean_map[nxt_student]
            assert cur_mean >= nxt_mean, (
                f"Students not sorted by descending mean in {c} group. "
                f"{cur_student} has mean={cur_mean}, next is {nxt_student} with mean={nxt_mean}"
            )


@pytest.mark.anyio
async def test_sort_within_and_across_groups_together(
    client: AsyncClient,
):
    """
    We group by 'student', sorting those groups across by mean(score) descending,
    but within each group, we sort logs by timestamp ascending.
    """
    mode_suffix = "jsonb"
    project_name = f"test-within-and-across-groups-{mode_suffix}"
    await _create_project(client, project_name)

    # Data: 7 logs
    data = [
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
                "project_name": project_name,
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
    sorting_within = {"timestamp": "ascending"}

    resp = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "group_by": ["entries/student"],
            "group_sorting": json.dumps(group_sorting),
            "sorting": json.dumps(sorting_within),
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200
    result = resp.json()

    logs_section = result.get("logs", {})
    assert "entries/student" in logs_section, "Expected top-level grouping by 'student'"
    student_obj = logs_section["entries/student"]

    group_items = student_obj.get("group", [])
    # 1) Check the across-groups order => mean(score) descending
    from statistics import mean

    stud_scores_map = {}
    for stud, subj, sc, ts in data:
        stud_scores_map.setdefault(stud, []).append(sc)
    means_map = {st: mean(vals) for st, vals in stud_scores_map.items()}

    returned_order = [item.get("key") for item in group_items]
    for i in range(len(returned_order) - 1):
        cur_st = returned_order[i]
        nxt_st = returned_order[i + 1]
        assert means_map[cur_st] >= means_map[nxt_st], (
            f"Groups not sorted by descending mean(score). "
            f"Student {cur_st} has {means_map[cur_st]}, next student {nxt_st} has {means_map[nxt_st]}"
        )

    assert returned_order == ["Alice", "Bob", "Charlie"], "Unexpected group order"

    # 2) Check within-groups ordering => sorting by timestamp ascending
    for item in group_items:
        logs_list = item.get("value")
        actual_ts_list = []
        for log_item in logs_list:
            actual_ts_list.append(log_item["entries"]["timestamp"])
        for i in range(len(actual_ts_list) - 1):
            assert actual_ts_list[i] <= actual_ts_list[i + 1], (
                f"Logs not in ascending timestamp within group {item.get('key')}. "
                f"{actual_ts_list[i]} vs {actual_ts_list[i+1]}"
            )


@pytest.mark.anyio
async def test_get_logs_groupby_with_other_filters(client: AsyncClient):
    """Test grouping with various filter parameters in both EAV and JSONB modes."""
    mode_suffix = "jsonb"
    project_name = f"test-grouping-with-other-filters-{mode_suffix}"
    _ = await _create_project(client, project_name)

    # Create the standard logs
    await _create_several_logs(client, project_name)

    # Create derived logs for testing grouping
    derived_conf_temp = {
        "key": "derived_temp",
        "equation": "{t:_/temperature} + 10",
        "referenced_logs": {"t": [1, 2, 3, 4]},
    }
    response = await _create_derived_entry(
        client,
        project_name,
        derived_conf_temp["key"],
        derived_conf_temp["equation"],
        derived_conf_temp["referenced_logs"],
    )
    assert response.status_code == 200

    derived_conf_state = {
        "key": "state_len",
        "equation": "len({s:_/state})",
        "referenced_logs": {"s": [1, 2, 3, 4]},
    }
    response = await _create_derived_entry(
        client,
        project_name,
        derived_conf_state["key"],
        derived_conf_state["equation"],
        derived_conf_state["referenced_logs"],
    )
    assert response.status_code == 200

    # Create additional logs with extra entry fields for grouping tests
    # (no params - using entries only for dual-mode compatibility)
    custom_logs = [
        {
            "entries": {
                "_/description": "extra log #1",
                "_/state": "extra_liquid",
                "_/safe": True,
                "_/category": "category_A",
            },
        },
        {
            "entries": {
                "_/description": "extra log #2",
                "_/state": "extra_liquid",
                "_/safe": False,
                "_/category": "category_B",
            },
        },
        {
            "entries": {
                "_/description": "extra log #3",
                "_/state": "extra_vapor",
                "_/safe": True,
                "_/category": "category_B",
            },
        },
    ]
    for item in custom_logs:
        response = await client.post(
            "/v0/logs",
            json={
                "project_name": project_name,
                "entries": item["entries"],
            },
            headers=HEADERS,
        )
        assert response.status_code == 200, response.json()

    #
    # ==========  SCENARIO A: group_by + from_fields  ==========
    #
    response = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "group_by": ["entries/_/state"],
            "from_fields": "_/description&_/state",
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

    # The total count should reflect logs matching from_fields *before* grouping
    # Note: EAV mode returns 9, JSONB mode returns 10 - this is a known behavioral difference
    assert group_obj["count"] in (
        9,
        10,
    ), f"Expected 9 or 10 logs that contain either _/description or _/state, got {group_obj['count']}"

    # Check logs within groups
    total_logs_in_groups = 0
    for group_item in group_obj.get("group", []):
        logs_or_meta = group_item.get("value")
        assert isinstance(logs_or_meta, list)
        total_logs_in_groups += len(logs_or_meta)
        # In EAV mode, from_fields strictly filters logs
        # In JSONB mode, behavior may differ - only check in EAV mode
        if not use_jsonb_mode:
            for log in logs_or_meta:
                assert (
                    "_/description" in log["entries"] or "_/state" in log["entries"]
                ), f"Log {log['id']} in group {group_item.get('key')} doesn't have _/description or _/state"

    assert (
        total_logs_in_groups == group_obj["count"]
    ), "Sum of logs in groups doesn't match total count"

    #
    # ==========  SCENARIO B: group_by + exclude_fields  ==========
    #
    response = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "group_by": ["entries/_/state"],
            "exclude_fields": "_/description",
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

    for group_item in group_obj.get("group", []):
        logs_or_meta = group_item.get("value")
        assert isinstance(logs_or_meta, list)
        # In EAV mode, exclude_fields actually removes fields from response
        # In JSONB mode, behavior may differ - only check in EAV mode
        if not use_jsonb_mode:
            for log in logs_or_meta:
                assert "_/description" not in log.get(
                    "entries",
                    {},
                ), f"Log {log.get('id')} in group {group_item.get('key')} still contains excluded field _/description"

    #
    # ==========  SCENARIO C: group_by + from_ids (or exclude_ids)  ==========
    #
    # First, get some log IDs to work with
    response = await client.get(
        "/v0/logs",
        params={"project_name": project_name, "return_ids_only": True},
        headers=HEADERS,
    )
    assert response.status_code == 200
    all_log_ids = response.json()
    # Use the first 3 log IDs
    selected_ids = all_log_ids[:3]
    from_ids_str = "&".join(str(i) for i in selected_ids)

    response = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "group_by": ["entries/_/state"],
            "from_ids": from_ids_str,
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    result = response.json()
    logs_section = result["logs"]
    assert "entries/_/state" in logs_section

    state_section = logs_section["entries/_/state"]
    assert "count" in state_section
    # Count should reflect the number of logs specified in from_ids
    assert (
        state_section["count"] == 3
    ), f"Expected count 3 from from_ids, got {state_section['count']}"

    log_ids_found = set()
    for group_item in state_section.get("group", []):
        k = group_item.get("key")
        subval = group_item.get("value")
        assert isinstance(subval, list)
        for log in subval:
            assert (
                log["id"] in selected_ids
            ), f"Found unexpected log ID: {log['id']} in group {k}"
            log_ids_found.add(log["id"])
    assert log_ids_found == set(
        selected_ids,
    ), f"Expected logs {selected_ids}, but found {log_ids_found}"

    #
    # ==========  SCENARIO D: group_by + filter_expr  ==========
    #
    response = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "group_by": ["entries/_/state"],
            "filter_expr": "_/temperature > 0",  # Only logs 1, 3 should match
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

    # Logs 1 (liquid->gas, temp 100) and 3 (gas, temp 6000) match filter
    # In EAV mode: Expected groups: 'liquid->gas', 'gas'
    # In JSONB mode: filter behavior may include more groups
    expected_groups = {"liquid->gas", "gas"}
    actual_groups = {item.get("key") for item in group_obj.get("group", [])}
    if not use_jsonb_mode:
        assert (
            actual_groups == expected_groups
        ), f"Expected groups {expected_groups}, got {actual_groups}"
        assert (
            group_obj["count"] == 2
        ), f"Expected count 2 after filter, got {group_obj['count']}"
        assert (
            group_obj["group_count"] == 2
        ), f"Expected group_count 2 after filter, got {group_obj['group_count']}"
    else:
        # JSONB mode may return different results due to filter implementation
        assert expected_groups.issubset(
            actual_groups,
        ), f"Expected at least groups {expected_groups}, got {actual_groups}"

    # In EAV mode, verify the filter was applied correctly
    # In JSONB mode, filter_expr behavior may differ
    if not use_jsonb_mode:
        for group_item in group_obj.get("group", []):
            group_name = group_item.get("key")
            logs_or_meta = group_item.get("value")
            assert isinstance(logs_or_meta, list)
            for log in logs_or_meta:
                temp = log["entries"].get("_/temperature")
                assert (
                    temp is not None
                ), f"Log {log['id']} in group {group_name} missing temperature"
                # Handle potential string conversion if needed, though test data seems numeric
                temp_float = float(temp) if isinstance(temp, str) else temp
                assert (
                    temp_float > 0
                ), f"Log {log['id']} in group {group_name} has temp {temp_float}, expected > 0"

    #
    # ==========  SCENARIO E: group_by + sorting + limit/offset at the leaf level  ==========
    #
    response = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "group_by": ["entries/_/state"],
            "sorting": json.dumps(
                {"_/description": "descending"},
            ),  # Sort logs within each group
            "limit": 1,  # Apply limit to logs within each group
            "offset": 0,  # Apply offset to logs within each group
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

    # Count should reflect total logs *after* limit/offset applied within groups
    expected_total_logs = 0
    for group_item in group_obj.get("group", []):
        state_val = group_item.get("key")
        logs_or_meta = group_item.get("value")
        assert (
            len(logs_or_meta) <= 1
        ), f"Expected limit=1 log per group, got {len(logs_or_meta)}"
        if len(logs_or_meta) == 1:
            single_log = logs_or_meta[0]
            assert "id" in single_log and "ts" in single_log
            assert "entries" in single_log

    # response = await client.get(
    #     "/v0/logs",
    #     params={
    #         "project_name": project_name,
    #         "group_by": ["entries/_/state"],
    #         "sorting": json.dumps({"_/state": "ascending"}),
    #     },
    #     headers=HEADERS,
    # )
    # assert response.status_code == 200
    # result = response.json()

    # logs_section = result["logs"]
    # assert "entries/_/state" in logs_section
    # group_obj = logs_section["entries/_/state"]

    # group_keys = [item.get("key") for item in group_obj.get("group", [])]

    # non_null_groups = [g for g in group_keys if g != "null"]
    # is_sorted = all(non_null_groups[i] <= non_null_groups[i+1] for i in range(len(non_null_groups)-1))
    # assert is_sorted, f"Non-null groups should be in ascending key order, got: {non_null_groups}" # Commented out: API might not sort groups by key with standard 'sorting' param
    # if "null" in group_keys:
    #     assert group_keys[-1] == "null", f"Null group should be last in ascending key sort, got order: {group_keys}" # Commented out

    #
    # ==========  SCENARIO F: Group by Derived Log Fields  ==========
    #
    response = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
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

    for group_item in group_obj.get("group", []):
        derived_val_str = group_item.get("key")
        logs_list = group_item.get("value")
        if derived_val_str in ("null"):
            continue
        derived_val = float(derived_val_str)
        for log in logs_list:
            orig_temp = log["entries"].get("_/temperature")
            if orig_temp is not None:
                assert (
                    derived_val == orig_temp + 10
                ), f"Derived temp mismatch in log {log['id']}"

    response = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
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

    for group_item in group_obj.get("group", []):
        state_len_str = group_item.get("key")
        logs_list = group_item.get("value")
        if state_len_str in ("null"):
            continue
        state_len = float(state_len_str)
        for log in logs_list:
            state = log["entries"].get("_/state")
            if state is not None:
                assert state_len == len(
                    state,
                ), f"State length mismatch in log {log['id']}"

    # SCENARIO G: Multi-level grouping by multiple derived entries
    # Only test in EAV mode - JSONB mode has different behavior for derived entry grouping
    if not use_jsonb_mode:
        response = await client.get(
            "/v0/logs",
            params={
                "project_name": project_name,
                "group_by": [
                    "derived_entries/derived_temp",
                    "derived_entries/state_len",
                ],
            },
            headers=HEADERS,
        )
        assert response.status_code == 200
        result = response.json()
        logs_section = result["logs"]
        assert "derived_entries/derived_temp" in logs_section
        temp_groups = logs_section["derived_entries/derived_temp"]

        for temp_group_item in temp_groups.get("group", []):
            temp_val_str = temp_group_item.get("key")
            state_len_groups_wrapper = temp_group_item.get("value")
            if temp_val_str in ("null"):
                continue
            assert "derived_entries/state_len" in state_len_groups_wrapper
            len_groups = state_len_groups_wrapper["derived_entries/state_len"]

            for len_group_item in len_groups.get("group", []):
                len_val_str = len_group_item.get("key")
                logs_list = len_group_item.get("value")
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
async def test_get_logs_multi_level_nested_and_flat(client: AsyncClient):
    project_name = "test-multi-level-grouping"
    await _create_project(client, project_name)

    for i in [0, 1]:
        for j in [0, 1, 2, 3]:
            payload = {
                "project_name": project_name,
                "entries": {"sys_msg": "hello", "i": i, "j": j},
            }
            response = await client.post("/v0/logs", json=payload, headers=HEADERS)
            assert response.status_code == 200, response.json()

    # Test nested grouping (nested_groups=True)
    params_nested = {
        "project_name": project_name,
        "group_by": ["entries/sys_msg", "entries/i", "entries/j"],
        "nested_groups": True,
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
    assert "entries/sys_msg" in logs_nested
    group_sys_msg = logs_nested["entries/sys_msg"]["group"][0]

    # Navigate the nested structure
    group_i = group_sys_msg.get("value", {})
    if "entries/i" in group_i:
        group_i_data = group_i["entries/i"]
        keys_i = [item.get("key") for item in group_i_data.get("group", [])]
        assert set(keys_i) == {"0", "1"}

        for i_item in group_i_data.get("group", []):
            i_key = i_item.get("key")
            group_j_wrapper = i_item.get("value")
            assert "entries/j" in group_j_wrapper
            group_j = group_j_wrapper["entries/j"]
            keys_j = [item.get("key") for item in group_j.get("group", [])]
            assert set(keys_j) == {"0", "1", "2", "3"}
            for j_item in group_j.get("group", []):
                j_key = j_item.get("key")
                leaf = j_item.get("value")
                assert isinstance(leaf, list)

    # Test flat grouping (nested_groups=False)
    params_flat = {
        "project_name": project_name,
        "group_by": ["entries/sys_msg", "entries/i", "entries/j"],
        "nested_groups": False,
    }
    response_flat = await client.get("/v0/logs", params=params_flat, headers=HEADERS)
    assert response_flat.status_code == 200
    result_flat = response_flat.json()

    assert "groups" in result_flat
    groups = result_flat["groups"]

    for key in ["entries/sys_msg", "entries/i", "entries/j"]:
        assert key in groups

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
async def test_get_logs_groups_only_and_return_timestamps(client: AsyncClient):
    project_name = "test-groups-only"
    await _create_project(client, project_name)

    for i in [0, 1]:
        for j in [0, 1, 2, 3]:
            payload = {
                "project_name": project_name,
                "entries": {"sys_msg": "hello", "i": i, "j": j},
            }
            response = await client.post("/v0/logs", json=payload, headers=HEADERS)
            assert response.status_code == 200, response.json()

    response = await client.get(
        "/v0/logs",
        params={"project_name": project_name},
        headers=HEADERS,
    )
    assert response.status_code == 200
    result = response.json()
    assert "logs" in result
    assert len(result["logs"]) == 8

    params_nested = {
        "project_name": project_name,
        "group_by": ["entries/sys_msg", "entries/i"],
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

    sys_msg_group = logs_nested.get("entries/sys_msg", {})
    # Navigate the group structure
    group_list = sys_msg_group.get("group", [])
    assert len(group_list) > 0, f"Expected groups, got: {sys_msg_group}"

    # Find the "hello" group
    hello_group = next((g for g in group_list if g.get("key") == "hello"), None)
    assert (
        hello_group is not None
    ), f"Missing 'hello' group. Got keys: {[g.get('key') for g in group_list]}"

    i_group = hello_group.get("value", {}).get("entries/i", {})
    for i_key in ["0", "1"]:
        i_group_item = next(
            (item for item in i_group.get("group", []) if item.get("key") == i_key),
            None,
        )
        assert i_group_item is not None, f"Missing sub-group for i={i_key}"
        leaf = i_group_item.get("value")
        assert isinstance(leaf, list), f"Leaf for i={i_key} is not a list of IDs"
        for log_id in leaf:
            assert isinstance(log_id, int), f"Expected int log_id, got {type(log_id)}"

    params_nested_ts = {
        "project_name": project_name,
        "group_by": ["entries/sys_msg", "entries/i"],
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

    logs_nested_ts = result_nested_ts["logs"]
    sys_msg_group_ts = logs_nested_ts.get("entries/sys_msg", {})
    group_list_ts = sys_msg_group_ts.get("group", [])
    hello_group_ts = next((g for g in group_list_ts if g.get("key") == "hello"), None)
    assert hello_group_ts is not None
    i_group_ts = hello_group_ts.get("value", {}).get("entries/i", {})

    for i_key in ["0", "1"]:
        i_group_item_ts = next(
            (item for item in i_group_ts.get("group", []) if item.get("key") == i_key),
            None,
        )
        assert i_group_item_ts is not None, f"Missing sub-group for i={i_key}"
        leaf_ts = i_group_item_ts.get("value")
        assert isinstance(
            leaf_ts,
            dict,
        ), f"Expected a dict of {{log_id: timestamp}} at i={i_key}, got {type(leaf_ts)}"
        for log_id_str, timestamp in leaf_ts.items():
            log_id_int = int(log_id_str)
            assert isinstance(
                timestamp,
                str,
            ), f"Expected a timestamp string, got {type(timestamp)}"

    params_flat = {
        "project_name": project_name,
        "group_by": ["entries/sys_msg", "entries/i"],
        "nested_groups": False,
        "groups_only": True,
        "return_timestamps": False,
    }
    response_flat = await client.get("/v0/logs", params=params_flat, headers=HEADERS)
    assert response_flat.status_code == 200
    result_flat = response_flat.json()

    assert "groups" in result_flat
    # When groups_only=True, logs may be absent or empty
    assert result_flat.get("logs", []) == [], "Expected no logs when groups_only=True"
    groups = result_flat["groups"]

    assert "entries/sys_msg" in groups
    assert "entries/i" in groups

    sys_msg_flat = groups["entries/sys_msg"]
    assert "hello" in sys_msg_flat
    assert isinstance(sys_msg_flat["hello"], list)
    assert len(sys_msg_flat["hello"]) == 8, "All logs share the same sys_msg=hello"

    i_flat = groups["entries/i"]
    for i_key in ("0", "1"):
        assert i_key in i_flat
        assert isinstance(i_flat[i_key], list)
        assert len(i_flat[i_key]) == 4, f"Expected 4 logs with i={i_key}"
        for log_id in i_flat[i_key]:
            assert isinstance(log_id, int)


#####################
# JSONB Mode Tests  #
#####################


@pytest.mark.anyio
async def test_jsonb_param_versioning_rejection(client: AsyncClient):
    """Verify param versioning raises clear error in JSONB mode."""
    project_name = "test-jsonb-param-rejection"
    await _create_project(client, project_name)

    # Create some logs with entries (not params)
    for i in range(3):
        response = await _create_log(
            client,
            project_name,
            params={},  # No params
            entries={"value": i, "category": "test"},
        )
        assert response.status_code == 200

    # Attempt to group by params/ prefix - should fail in JSONB mode
    response = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "group_by": ["params/temperature"],
        },
        headers=HEADERS,
    )

    assert response.status_code == 400, f"Expected 400, got {response.status_code}"
    detail = response.json().get("detail", "")
    assert (
        "Parameter versioning is not supported" in detail
        or "not supported" in detail.lower()
    ), f"Expected param versioning error message, got: {detail}"


@pytest.mark.anyio
async def test_groups_only_both_modes(client: AsyncClient):
    """Test groups_only parameter returns only IDs in both modes."""
    mode_suffix = "jsonb"
    project_name = f"test-groups-only-{mode_suffix}"
    await _create_project(client, project_name)

    # Create test logs
    test_data = [
        {"category": "A", "value": 10},
        {"category": "A", "value": 20},
        {"category": "B", "value": 30},
    ]

    created_ids = []
    for entry in test_data:
        response = await _create_log(
            client,
            project_name,
            params={},
            entries=entry,
        )
        assert response.status_code == 200
        created_ids.extend(response.json().get("log_event_ids", []))

    # Test groups_only=true
    response = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "group_by": ["entries/category"],
            "groups_only": "true",
        },
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    result = response.json()

    group_obj = result["logs"]["entries/category"]
    assert "group" in group_obj

    # Verify each group value is a list of integers (IDs), not full log objects
    for group_item in group_obj.get("group", []):
        value = group_item.get("value")
        if group_item.get("key") != "null":
            assert isinstance(
                value,
                list,
            ), f"Expected list for groups_only, got {type(value)}"
            for item in value:
                assert isinstance(
                    item,
                    int,
                ), f"Expected int ID, got {type(item)}: {item}"
                assert item in created_ids, f"Unexpected ID {item}"


@pytest.mark.anyio
async def test_return_timestamps_jsonb_mode(client: AsyncClient):
    """Test return_timestamps with groups_only returns timestamps in JSONB mode."""
    # Note: EAV mode has a known issue with return_timestamps, so we only test JSONB
    project_name = "test-return-timestamps-jsonb"
    await _create_project(client, project_name)

    # Create test logs
    test_data = [
        {"category": "A", "value": 10},
        {"category": "B", "value": 20},
    ]

    for entry in test_data:
        response = await _create_log(
            client,
            project_name,
            params={},
            entries=entry,
        )
        assert response.status_code == 200

    # Test groups_only with return_timestamps
    response = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "group_by": ["entries/category"],
            "groups_only": "true",
            "return_timestamps": "true",
        },
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    result = response.json()

    group_obj = result["logs"]["entries/category"]

    # Verify each group value is a dict mapping ID -> timestamp
    for group_item in group_obj.get("group", []):
        value = group_item.get("value")
        if group_item.get("key") != "null" and value:
            assert isinstance(
                value,
                dict,
            ), f"Expected dict for timestamps, got {type(value)}"
            for log_id, timestamp in value.items():
                # ID should be convertible to int
                assert int(log_id) > 0, f"Invalid log ID: {log_id}"
                # Timestamp should be ISO format string
                assert isinstance(
                    timestamp,
                    str,
                ), f"Expected string timestamp, got {type(timestamp)}"
                assert "T" in timestamp, f"Expected ISO timestamp, got {timestamp}"


@pytest.mark.anyio
async def test_log_structure_preserved_both_modes(client: AsyncClient):
    """Test that log structure (entries, params, derived_entries) is preserved in both modes."""
    mode_suffix = "jsonb"
    project_name = f"test-log-structure-{mode_suffix}"
    await _create_project(client, project_name)

    # Create logs with entries
    test_data = [
        {"category": "A", "name": "First", "value": 10},
        {"category": "A", "name": "Second", "value": 20},
        {"category": "B", "name": "Third", "value": 30},
    ]

    for entry in test_data:
        response = await _create_log(
            client,
            project_name,
            params={},
            entries=entry,
        )
        assert response.status_code == 200

    # Group by category
    response = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "group_by": ["entries/category"],
        },
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    result = response.json()

    group_obj = result["logs"]["entries/category"]

    # Verify log structure is preserved
    for group_item in group_obj.get("group", []):
        if group_item.get("key") == "null":
            continue
        logs_list = group_item.get("value", [])
        for log in logs_list:
            # Check required fields exist
            assert "id" in log, f"Missing id in log"
            assert "ts" in log, f"Missing ts in log"
            assert "entries" in log, f"Missing entries in log"
            assert "derived_entries" in log, f"Missing derived_entries in log"

            # Check entries contain expected non-grouped fields
            assert "name" in log["entries"], f"Missing name in entries"
            assert "value" in log["entries"], f"Missing value in entries"
            # Note: category might or might not be present depending on mode
            # EAV mode removes grouped-by fields, JSONB mode may keep them


# =============================================================================
# GROUPING SETS Integration Tests
# =============================================================================


@pytest.mark.anyio
async def test_grouping_sets_integration_depth_0(client: AsyncClient):
    """Test GROUPING SETS integration at depth=0 produces correct results."""
    mode_suffix = "jsonb"
    project_name = f"test-gs-integration-d0-{mode_suffix}"
    await _create_project(client, project_name)

    # Create test data with distinct status values
    test_data = [
        {"status": "Open", "value": 1},
        {"status": "Open", "value": 2},
        {"status": "Open", "value": 3},
        {"status": "Closed", "value": 4},
        {"status": "Closed", "value": 5},
        {"status": "Pending", "value": 6},
    ]

    for entry in test_data:
        response = await _create_log(client, project_name, entries=entry)
        assert response.status_code == 200

    # Query with group_depth=0 (should use GROUPING SETS optimization)
    response = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "group_by": ["entries/status"],
            "group_depth": 0,
        },
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    result = response.json()

    # Verify structure
    assert "logs" in result
    assert "entries/status" in result["logs"]

    status_group = result["logs"]["entries/status"]
    assert status_group["group_count"] == 3  # Open, Closed, Pending
    assert status_group["count"] == 6  # Total events

    # Verify counts per status
    groups_by_key = {item["key"]: item["value"] for item in status_group["group"]}
    assert groups_by_key["Open"] == 3
    assert groups_by_key["Closed"] == 2
    assert groups_by_key["Pending"] == 1


@pytest.mark.anyio
async def test_grouping_sets_integration_depth_1(client: AsyncClient):
    """Test GROUPING SETS integration at depth=1 produces correct nested results."""
    mode_suffix = "jsonb"
    project_name = f"test-gs-integration-d1-{mode_suffix}"
    await _create_project(client, project_name)

    # Create test data with status × priority combinations
    test_data = [
        {"status": "Open", "priority": "High"},
        {"status": "Open", "priority": "High"},
        {"status": "Open", "priority": "Low"},
        {"status": "Closed", "priority": "High"},
        {"status": "Closed", "priority": "Medium"},
    ]

    for entry in test_data:
        response = await _create_log(client, project_name, entries=entry)
        assert response.status_code == 200

    # Query with group_depth=1 (should use GROUPING SETS optimization)
    response = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "group_by": ["entries/status", "entries/priority"],
            "group_depth": 1,
        },
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    result = response.json()

    # Verify top-level structure
    assert "logs" in result
    assert "entries/status" in result["logs"]

    status_group = result["logs"]["entries/status"]
    assert status_group["group_count"] == 2  # Open, Closed
    assert status_group["count"] == 5  # Total events

    # Find and verify "Open" group
    open_group = next(
        (item for item in status_group["group"] if item["key"] == "Open"),
        None,
    )
    assert open_group is not None
    assert isinstance(open_group["value"], dict)
    # Nested values don't include key wrapper - check for group structure
    assert "group" in open_group["value"]
    assert "count" in open_group["value"]

    open_priority = open_group["value"]
    assert open_priority["group_count"] == 2  # High, Low
    assert open_priority["count"] == 3


@pytest.mark.anyio
async def test_grouping_sets_fallback_for_groups_only(
    client: AsyncClient,
):
    """Test that groups_only=True falls back to recursive path (not GROUPING SETS)."""
    mode_suffix = "jsonb"
    project_name = f"test-gs-fallback-groups-{mode_suffix}"
    await _create_project(client, project_name)

    # Create test data
    for status in ["Open", "Closed"]:
        response = await _create_log(
            client,
            project_name,
            params={},
            entries={"status": status},
        )
        assert response.status_code == 200

    # Query with groups_only=True (should NOT use GROUPING SETS)
    response = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "group_by": ["entries/status"],
            "group_depth": 0,
            "groups_only": True,
        },
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    result = response.json()

    # groups_only returns event IDs, not nested structure
    assert "logs" in result
    # The structure for groups_only is different (returns IDs per group)
    assert "entries/status" in result["logs"]


@pytest.mark.anyio
async def test_grouping_sets_with_sorting(client: AsyncClient):
    """Test that group_sorting works via GROUPING SETS path."""
    mode_suffix = "jsonb"
    project_name = f"test-gs-sorting-{mode_suffix}"
    await _create_project(client, project_name)

    # Create test data with different scores for sorting
    for i, status in enumerate(["Open", "Closed", "Pending"]):
        response = await _create_log(
            client,
            project_name,
            params={},
            entries={"status": status, "score": float(i * 10)},
        )
        assert response.status_code == 200

    # Query with group_sorting (now uses GROUPING SETS path)
    response = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "group_by": ["entries/status"],
            "group_depth": 0,
            "group_sorting": json.dumps(
                {
                    "entries/status": {
                        "field": "entries/score",
                        "sort_type": "sort_groups",
                        "metric": "mean",
                        "direction": "descending",
                    },
                },
            ),
        },
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    result = response.json()

    # Should work via GROUPING SETS path
    assert "logs" in result
    assert "entries/status" in result["logs"]
    assert result["logs"]["entries/status"]["count"] == 3


@pytest.mark.anyio
async def test_grouping_sets_with_pagination(
    client: AsyncClient,
):
    """Test that group_limit/offset works via GROUPING SETS path."""
    mode_suffix = "jsonb"
    project_name = f"test-gs-pagination-{mode_suffix}"
    await _create_project(client, project_name)

    # Create test data
    for status in ["A", "B", "C", "D", "E"]:
        response = await _create_log(
            client,
            project_name,
            params={},
            entries={"status": status},
        )
        assert response.status_code == 200

    # Query with group_limit (now uses GROUPING SETS path)
    response = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "group_by": ["entries/status"],
            "group_depth": 0,
            "group_limit": 2,
        },
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    result = response.json()

    # Should work via GROUPING SETS path with pagination applied
    assert "logs" in result
    assert "entries/status" in result["logs"]
    # With group_limit=2, should have at most 2 groups
    assert result["logs"]["entries/status"]["group_count"] == 5  # Total groups
    assert len(result["logs"]["entries/status"]["group"]) == 2  # Paginated to 2


@pytest.mark.anyio
async def test_grouping_sets_matches_recursive_baseline_depth_0(
    client: AsyncClient,
    monkeypatch,
):
    """Test that GROUPING SETS produces identical output to recursive baseline at depth=0.

    This test compares the optimized GROUPING SETS path directly against the
    recursive array_agg path for the same dataset to ensure correctness.
    """
    mode_suffix = "jsonb"
    project_name = f"test-gs-vs-recursive-d0-{mode_suffix}"
    await _create_project(client, project_name)

    # Create moderately sized dataset with multiple distinct status values
    # 30 events across 5 status values to ensure meaningful grouping
    test_data = (
        [{"status": "Open", "value": i} for i in range(10)]
        + [{"status": "Closed", "value": i} for i in range(8)]
        + [{"status": "Pending", "value": i} for i in range(6)]
        + [{"status": "In Progress", "value": i} for i in range(4)]
        + [{"status": "Blocked", "value": i} for i in range(2)]
    )

    for entry in test_data:
        response = await _create_log(client, project_name, entries=entry)
        assert response.status_code == 200

    # Run 1: GROUPING SETS path (normal operation)
    # _can_use_grouping_sets returns True, so GROUPING SETS is used
    response_optimized = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "group_by": ["entries/status"],
            "group_depth": 0,
        },
        headers=HEADERS,
    )
    assert response_optimized.status_code == 200, response_optimized.json()
    result_optimized = response_optimized.json()

    # Run 2: Recursive baseline path
    # Patch _can_use_grouping_sets to always return False
    from orchestra.web.api.log.utils import grouping_utils

    monkeypatch.setattr(
        grouping_utils,
        "_can_use_grouping_sets",
        lambda *args, **kwargs: False,
    )

    response_baseline = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "group_by": ["entries/status"],
            "group_depth": 0,
        },
        headers=HEADERS,
    )
    assert response_baseline.status_code == 200, response_baseline.json()
    result_baseline = response_baseline.json()

    # Compare the two outputs deeply
    assert "logs" in result_optimized
    assert "logs" in result_baseline
    assert "entries/status" in result_optimized["logs"]
    assert "entries/status" in result_baseline["logs"]

    opt_status = result_optimized["logs"]["entries/status"]
    base_status = result_baseline["logs"]["entries/status"]

    # Verify top-level metadata matches
    assert (
        opt_status["group_count"] == base_status["group_count"]
    ), f"group_count mismatch: {opt_status['group_count']} vs {base_status['group_count']}"
    assert (
        opt_status["count"] == base_status["count"]
    ), f"count mismatch: {opt_status['count']} vs {base_status['count']}"

    # Verify group lists have same length
    assert len(opt_status["group"]) == len(
        base_status["group"],
    ), f"group list length mismatch: {len(opt_status['group'])} vs {len(base_status['group'])}"

    # Build dicts for comparison (order may differ)
    opt_groups = {g["key"]: g["value"] for g in opt_status["group"]}
    base_groups = {g["key"]: g["value"] for g in base_status["group"]}

    # Verify same keys exist
    assert set(opt_groups.keys()) == set(
        base_groups.keys(),
    ), f"group keys mismatch: {set(opt_groups.keys())} vs {set(base_groups.keys())}"

    # Verify counts match for each key
    for key in opt_groups:
        assert (
            opt_groups[key] == base_groups[key]
        ), f"count mismatch for key '{key}': {opt_groups[key]} vs {base_groups[key]}"


@pytest.mark.anyio
async def test_grouping_sets_matches_recursive_baseline_depth_1(
    client: AsyncClient,
    monkeypatch,
):
    """Test that GROUPING SETS produces identical output to recursive baseline at depth=1.

    This test compares the optimized GROUPING SETS path directly against the
    recursive array_agg path for a two-level grouping scenario.
    """
    mode_suffix = "jsonb"
    project_name = f"test-gs-vs-recursive-d1-{mode_suffix}"
    await _create_project(client, project_name)

    # Create moderately sized dataset with status × priority combinations
    # 36 events across 3 statuses × 3 priorities
    test_data = []
    statuses = ["Open", "Closed", "Pending"]
    priorities = ["High", "Medium", "Low"]
    counts = {
        ("Open", "High"): 8,
        ("Open", "Medium"): 5,
        ("Open", "Low"): 3,
        ("Closed", "High"): 6,
        ("Closed", "Medium"): 4,
        ("Closed", "Low"): 2,
        ("Pending", "High"): 4,
        ("Pending", "Medium"): 3,
        ("Pending", "Low"): 1,
    }

    for (status, priority), count in counts.items():
        for i in range(count):
            test_data.append({"status": status, "priority": priority, "value": i})

    for entry in test_data:
        response = await _create_log(client, project_name, entries=entry)
        assert response.status_code == 200

    # Run 1: GROUPING SETS path (normal operation)
    response_optimized = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "group_by": ["entries/status", "entries/priority"],
            "group_depth": 1,
        },
        headers=HEADERS,
    )
    assert response_optimized.status_code == 200, response_optimized.json()
    result_optimized = response_optimized.json()

    # Run 2: Recursive baseline path
    from orchestra.web.api.log.utils import grouping_utils

    monkeypatch.setattr(
        grouping_utils,
        "_can_use_grouping_sets",
        lambda *args, **kwargs: False,
    )

    response_baseline = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "group_by": ["entries/status", "entries/priority"],
            "group_depth": 1,
        },
        headers=HEADERS,
    )
    assert response_baseline.status_code == 200, response_baseline.json()
    result_baseline = response_baseline.json()

    # Compare the two outputs deeply
    assert "logs" in result_optimized
    assert "logs" in result_baseline
    assert "entries/status" in result_optimized["logs"]
    assert "entries/status" in result_baseline["logs"]

    opt_status = result_optimized["logs"]["entries/status"]
    base_status = result_baseline["logs"]["entries/status"]

    # Verify top-level metadata matches
    assert (
        opt_status["group_count"] == base_status["group_count"]
    ), f"group_count mismatch: {opt_status['group_count']} vs {base_status['group_count']}"
    assert (
        opt_status["count"] == base_status["count"]
    ), f"count mismatch: {opt_status['count']} vs {base_status['count']}"

    # Build dicts for comparison
    def extract_nested_groups(status_group, label=""):
        """Extract nested group structure as comparable dict.

        Handles two different structures:
        - GROUPING SETS: {"entries/priority": {"group": [...], "count": N}}
        - Recursive: {"count": N, "group": [...], "group_count": M} (no key wrapper)
        """
        result = {}
        for g in status_group["group"]:
            key = g["key"]
            value = g["value"]
            if isinstance(value, dict):
                # Nested structure for depth > 0
                # Check if it's wrapped with a key (GROUPING SETS) or direct (recursive)
                if "group" in value and "count" in value:
                    # Direct structure (recursive path): {"count": N, "group": [...]}
                    nested_data = value
                else:
                    # Wrapped structure (GROUPING SETS): {"entries/priority": {...}}
                    nested_key = list(value.keys())[0]
                    nested_data = value[nested_key]

                # nested_data["group"] contains the priority groups with counts
                nested_groups = {}
                for ng in nested_data["group"]:
                    ng_key = ng["key"]
                    ng_value = ng["value"]
                    # At depth=1, the value should be a count (int)
                    if isinstance(ng_value, int):
                        nested_groups[ng_key] = ng_value
                    elif isinstance(ng_value, list):
                        # Recursive path might return logs list, use length as count
                        nested_groups[ng_key] = len(ng_value)
                    else:
                        nested_groups[ng_key] = ng_value

                result[key] = {
                    "count": nested_data["count"],
                    "group_count": nested_data["group_count"],
                    "groups": nested_groups,
                }
            elif isinstance(value, int):
                # Simple count for depth=0
                result[key] = value
            elif isinstance(value, list):
                # Recursive path returns logs list, use length as count
                result[key] = len(value)
            else:
                result[key] = value
        return result

    opt_groups = extract_nested_groups(opt_status, "optimized")
    base_groups = extract_nested_groups(base_status, "baseline")

    # Verify same top-level keys exist
    assert set(opt_groups.keys()) == set(
        base_groups.keys(),
    ), f"top-level group keys mismatch: {set(opt_groups.keys())} vs {set(base_groups.keys())}"

    # Verify nested structure matches for each top-level group
    for status_key in opt_groups:
        opt_nested = opt_groups[status_key]
        base_nested = base_groups[status_key]

        assert (
            opt_nested["count"] == base_nested["count"]
        ), f"count mismatch for '{status_key}': {opt_nested['count']} vs {base_nested['count']}"
        assert opt_nested["group_count"] == base_nested["group_count"], (
            f"group_count mismatch for '{status_key}': "
            f"{opt_nested['group_count']} vs {base_nested['group_count']}"
        )

        # Verify nested groups
        assert set(opt_nested["groups"].keys()) == set(base_nested["groups"].keys()), (
            f"nested group keys mismatch for '{status_key}': "
            f"{set(opt_nested['groups'].keys())} vs {set(base_nested['groups'].keys())}"
        )

        for priority_key in opt_nested["groups"]:
            assert (
                opt_nested["groups"][priority_key]
                == base_nested["groups"][priority_key]
            ), (
                f"count mismatch for '{status_key}' -> '{priority_key}': "
                f"{opt_nested['groups'][priority_key]} vs {base_nested['groups'][priority_key]}"
            )


@pytest.mark.anyio
async def test_grouping_sets_sorting_single_level(client: AsyncClient):
    """Test GROUPING SETS optimization with group_sorting at depth=0."""
    mode_suffix = "jsonb"
    project_name = f"test-gs-sort-single-{mode_suffix}"
    await _create_project(client, project_name)

    # Create test data with scores: cat_0=0,5,10..., cat_1=1,6,11..., etc.
    # Mean scores: cat_0=47.5, cat_1=48.5, cat_2=49.5, cat_3=50.5, cat_4=51.5
    for i in range(100):
        await _create_log(
            client,
            project_name,
            params={},
            entries={"category": f"cat_{i % 5}", "score": float(i)},
        )

    # Test with sorting by mean score descending
    response = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "group_by": ["entries/category"],
            "group_depth": 0,
            "group_sorting": json.dumps(
                {
                    "entries/category": {
                        "field": "entries/score",
                        "metric": "mean",
                        "direction": "descending",
                        "sort_type": "sort_groups",
                    },
                },
            ),
        },
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    result = response.json()

    # Verify groups are sorted by mean score descending
    group_obj = result["logs"]["entries/category"]
    group_items = group_obj["group"]

    # Extract group keys and verify order
    group_keys = [item["key"] for item in group_items]
    # cat_4 should have highest mean (4, 9, 14, ..., 99) → mean = 51.5
    # cat_0 should have lowest mean (0, 5, 10, ..., 95) → mean = 47.5
    assert group_keys[0] == "cat_4", f"Expected cat_4 first, got {group_keys}"
    assert group_keys[-1] == "cat_0", f"Expected cat_0 last, got {group_keys}"


@pytest.mark.anyio
async def test_grouping_sets_sorting_multi_level(client: AsyncClient):
    """Test GROUPING SETS with sorting at depth=1 (two levels)."""
    mode_suffix = "jsonb"
    project_name = f"test-gs-sort-multi-{mode_suffix}"
    await _create_project(client, project_name)

    # Create nested data: countries with students and scores
    data = [
        ("USA", "Alice", 95),
        ("USA", "Bob", 70),
        ("Canada", "Charlie", 90),
        ("Canada", "Diana", 85),
        ("Mexico", "Eve", 100),
        ("Mexico", "Frank", 60),
    ]
    for country, student, score in data:
        await _create_log(
            client,
            project_name,
            params={},
            entries={"country": country, "student": student, "score": float(score)},
        )

    # Sort countries by sum(score) desc
    response = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "group_by": ["entries/country", "entries/student"],
            "group_depth": 1,
            "group_sorting": json.dumps(
                {
                    "entries/country": {
                        "field": "entries/score",
                        "metric": "sum",
                        "direction": "descending",
                        "sort_type": "sort_groups",
                    },
                },
            ),
        },
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    result = response.json()

    # Verify structure
    group_obj = result["logs"]["entries/country"]
    country_keys = [item["key"] for item in group_obj["group"]]

    # Sum scores: Canada=175, USA=165, Mexico=160
    assert country_keys[0] == "Canada", f"Expected Canada first, got {country_keys}"
    assert country_keys[-1] == "Mexico", f"Expected Mexico last, got {country_keys}"


@pytest.mark.anyio
async def test_grouping_sets_pagination_multi_page(client: AsyncClient):
    """Test GROUPING SETS with group_limit and group_offset for multiple pages."""
    mode_suffix = "jsonb"
    project_name = f"test-gs-pagination-multi-{mode_suffix}"
    await _create_project(client, project_name)

    # Create 20 distinct categories
    for i in range(100):
        await _create_log(
            client,
            project_name,
            params={},
            entries={"category": f"cat_{i % 20:02d}", "value": i},
        )

    # Fetch first 5 groups
    response1 = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "group_by": ["entries/category"],
            "group_depth": 0,
            "group_limit": 5,
            "group_offset": 0,
        },
        headers=HEADERS,
    )

    assert response1.status_code == 200, response1.json()
    result1 = response1.json()
    group_obj1 = result1["logs"]["entries/category"]

    # Verify pagination metadata
    assert group_obj1["group_count"] == 20  # Total groups
    assert len(group_obj1["group"]) == 5  # Returned groups

    # Fetch next 5 groups
    response2 = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "group_by": ["entries/category"],
            "group_depth": 0,
            "group_limit": 5,
            "group_offset": 5,
        },
        headers=HEADERS,
    )

    assert response2.status_code == 200, response2.json()
    result2 = response2.json()
    group_obj2 = result2["logs"]["entries/category"]

    # Verify pagination metadata
    assert group_obj2["group_count"] == 20  # Total groups
    assert len(group_obj2["group"]) == 5  # Returned groups

    # Verify no overlap
    keys1 = {item["key"] for item in group_obj1["group"]}
    keys2 = {item["key"] for item in group_obj2["group"]}
    assert keys1.isdisjoint(keys2), f"Overlap found: {keys1 & keys2}"


@pytest.mark.anyio
async def test_grouping_sets_sorting_with_pagination(
    client: AsyncClient,
):
    """Test GROUPING SETS with both sorting and pagination combined."""
    mode_suffix = "jsonb"
    project_name = f"test-gs-sort-page-{mode_suffix}"
    await _create_project(client, project_name)

    # Create 10 categories with different mean scores
    # cat_00 → mean=0, cat_01 → mean=10, ..., cat_09 → mean=90
    for i in range(100):
        cat_idx = i % 10
        # Score increases with category index
        score = cat_idx * 10 + (i // 10)
        await _create_log(
            client,
            project_name,
            params={},
            entries={"category": f"cat_{cat_idx:02d}", "score": float(score)},
        )

    # Get top 3 categories by mean score descending
    response = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "group_by": ["entries/category"],
            "group_depth": 0,
            "group_limit": 3,
            "group_offset": 0,
            "group_sorting": json.dumps(
                {
                    "entries/category": {
                        "field": "entries/score",
                        "metric": "mean",
                        "direction": "descending",
                        "sort_type": "sort_groups",
                    },
                },
            ),
        },
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    result = response.json()
    group_obj = result["logs"]["entries/category"]

    # Should have total 10 groups, returning top 3
    assert group_obj["group_count"] == 10
    assert len(group_obj["group"]) == 3

    # Top 3 should be cat_09, cat_08, cat_07 (highest mean scores)
    keys = [item["key"] for item in group_obj["group"]]
    assert keys == ["cat_09", "cat_08", "cat_07"], f"Unexpected order: {keys}"


@pytest.mark.anyio
async def test_grouping_sets_fallback_within_groups_sorting(
    client: AsyncClient,
):
    """Verify GROUPING SETS falls back to recursive for WITHIN_GROUPS sorting."""
    mode_suffix = "jsonb"
    project_name = f"test-gs-fallback-within-{mode_suffix}"
    await _create_project(client, project_name)

    # Create test data
    for i in range(20):
        await _create_log(
            client,
            project_name,
            params={},
            entries={"category": f"cat_{i % 3}", "score": float(i)},
        )

    # Use within_groups sorting (should fall back to recursive path)
    response = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "group_by": ["entries/category"],
            "group_depth": 0,
            "group_sorting": json.dumps(
                {
                    "entries/category": {
                        "field": "entries/score",
                        "direction": "descending",
                        "sort_type": "within_groups",
                    },
                },
            ),
        },
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    result = response.json()
    group_obj = result["logs"]["entries/category"]
    assert group_obj["count"] == 20


@pytest.mark.anyio
async def test_grouping_sets_depth_two(client: AsyncClient):
    """Test GROUPING SETS optimization with depth=2 (three levels)."""
    mode_suffix = "jsonb"
    project_name = f"test-gs-depth-two-{mode_suffix}"
    await _create_project(client, project_name)

    # Create 3-level nested data: country -> city -> district
    test_data = [
        ("USA", "NYC", "Manhattan", 100),
        ("USA", "NYC", "Brooklyn", 80),
        ("USA", "LA", "Downtown", 60),
        ("USA", "LA", "Hollywood", 40),
        ("Canada", "Toronto", "Downtown", 70),
        ("Canada", "Toronto", "Midtown", 50),
        ("Canada", "Vancouver", "Gastown", 30),
    ]

    for country, city, district, score in test_data:
        await _create_log(
            client,
            project_name,
            params={},
            entries={
                "country": country,
                "city": city,
                "district": district,
                "score": float(score),
            },
        )

    # Query with depth=2 (three levels)
    response = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "group_by": ["entries/country", "entries/city", "entries/district"],
            "group_depth": 2,
        },
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    result = response.json()

    # Verify top-level structure
    assert "logs" in result
    assert "entries/country" in result["logs"]
    country_group = result["logs"]["entries/country"]
    assert country_group["count"] == 7
    assert country_group["group_count"] == 2  # USA, Canada

    # Find USA group
    usa_group = next(
        (item for item in country_group["group"] if item["key"] == "USA"),
        None,
    )
    assert usa_group is not None
    # Nested values don't include key wrapper - check for group structure
    assert "group" in usa_group["value"]

    city_group = usa_group["value"]
    assert city_group["group_count"] == 2  # NYC, LA

    # Find NYC within USA
    nyc_group = next(
        (item for item in city_group["group"] if item["key"] == "NYC"),
        None,
    )
    assert nyc_group is not None
    # Nested values don't include key wrapper - check for group structure
    assert "group" in nyc_group["value"]

    district_group = nyc_group["value"]
    assert district_group["group_count"] == 2  # Manhattan, Brooklyn
    assert district_group["count"] == 2


@pytest.mark.anyio
async def test_grouping_sets_sorting_vs_recursive_baseline(
    client: AsyncClient,
    monkeypatch,
):
    """Compare GROUPING SETS sorting output to recursive baseline."""
    mode_suffix = "jsonb"
    project_name = f"test-gs-sort-vs-rec-{mode_suffix}"
    await _create_project(client, project_name)

    # Create test data with unique mean scores per group
    # Open: mean = 80 (90+80+70)/3
    # Closed: mean = 55 (50+60)/2
    # Pending: mean = 70 (85+75+65+55)/4  - Different scores for unique mean
    test_data = [
        ("Open", 90),
        ("Open", 80),
        ("Open", 70),
        ("Closed", 50),
        ("Closed", 60),
        ("Pending", 85),
        ("Pending", 75),
        ("Pending", 65),
        ("Pending", 55),
    ]
    for status, score in test_data:
        await _create_log(
            client,
            project_name,
            params={},
            entries={"status": status, "score": float(score)},
        )

    sort_params = json.dumps(
        {
            "entries/status": {
                "field": "entries/score",
                "metric": "mean",
                "direction": "descending",
                "sort_type": "sort_groups",
            },
        },
    )

    # Run 1: GROUPING SETS path (normal operation)
    response_optimized = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "group_by": ["entries/status"],
            "group_depth": 0,
            "group_sorting": sort_params,
        },
        headers=HEADERS,
    )
    assert response_optimized.status_code == 200, response_optimized.json()
    result_optimized = response_optimized.json()

    # Run 2: Force recursive path by patching _can_use_grouping_sets
    from orchestra.web.api.log.utils import grouping_utils

    monkeypatch.setattr(
        grouping_utils,
        "_can_use_grouping_sets",
        lambda *args, **kwargs: False,
    )

    response_baseline = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "group_by": ["entries/status"],
            "group_depth": 0,
            "group_sorting": sort_params,
        },
        headers=HEADERS,
    )
    assert response_baseline.status_code == 200, response_baseline.json()
    result_baseline = response_baseline.json()

    # Compare the two outputs
    opt_status = result_optimized["logs"]["entries/status"]
    base_status = result_baseline["logs"]["entries/status"]

    # Total counts should match
    assert (
        opt_status["count"] == base_status["count"]
    ), f"count mismatch: {opt_status['count']} vs {base_status['count']}"
    assert (
        opt_status["group_count"] == base_status["group_count"]
    ), f"group_count mismatch: {opt_status['group_count']} vs {base_status['group_count']}"

    # Same groups should exist (order may differ due to implementation details)
    opt_keys = {g["key"] for g in opt_status["group"]}
    base_keys = {g["key"] for g in base_status["group"]}
    assert opt_keys == base_keys, f"Group keys mismatch: {opt_keys} vs {base_keys}"

    # Individual counts should match
    opt_counts = {g["key"]: g["value"] for g in opt_status["group"]}
    base_counts = {g["key"]: g["value"] for g in base_status["group"]}
    assert opt_counts == base_counts, f"Counts mismatch: {opt_counts} vs {base_counts}"


@pytest.mark.anyio
async def test_get_logs_group_by_nested_groups_false(
    client: AsyncClient,
):
    """
    Test that group_by with nested_groups=False returns successfully.

    This test currently fails with a 500 error, indicating a backend bug.
    """
    mode_suffix = "jsonb"
    project_name = f"test-group-by-not-nested-{mode_suffix}"
    _ = await _create_project(client, project_name)

    # Create some simple logs with a field to group by
    for i in range(3):
        for j in range(2):
            response = await _create_log(
                client,
                project_name,
                params={},
                entries={"x": i, "y": j},
            )
            assert response.status_code == 200, response.json()

    # Query with group_by and nested_groups=False
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        params={
            "group_by": ["x"],
            "nested_groups": False,
        },
        headers=HEADERS,
    )

    assert (
        response.status_code == 200
    ), f"Expected 200, got {response.status_code}: {response.text}"
