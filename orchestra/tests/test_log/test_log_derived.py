import base64
import json
import os

import pytest
from httpx import AsyncClient

from . import (
    HEADERS,
    _create_derived_entry,
    _create_image_log,
    _create_log,
    _create_project,
    _create_several_logs,
    _delete_logs,
    wait_for_gcs_images,
)


@pytest.mark.anyio
async def test_create_derived_entry_with_list(client: AsyncClient):
    project_name = "test_project_list"
    await _create_project(client, project_name, user=1)

    # Create base logs
    log_ids = []
    for i in range(3):
        response = await _create_log(client, project_name, entries={"a": i * 10})
        assert response.status_code == 200
        log_ids.append(response.json()["log_event_ids"][0])

    # Create derived logs
    key = "half_a"
    equation = "{log1:a}*2"
    referenced_logs = {"log1": log_ids}
    response = await _create_derived_entry(
        client,
        project_name,
        key,
        equation,
        referenced_logs,
    )
    assert response.status_code == 200


@pytest.mark.anyio
async def test_derived_over_nested_containers(client: AsyncClient):
    project = "test_nested_containers"
    await _create_project(client, project, user=1)

    # lod = list of dicts
    entries = {"lod": [{"a": 1, "b": 3}, {"a": 2, "c": 6}, {"a": 3, "d": 1}]}
    resp = await _create_log(client, project, entries=entries)
    assert resp.status_code == 200
    log_id = resp.json()["log_event_ids"][0]

    # Derived: sum of a's
    # use list comp projection via python2SQL: [d['a'] for d in lod]
    eq = "sum([d['a'] for d in {log:lod}])"
    resp = await _create_derived_entry(
        client,
        project,
        key="sum_a",
        equation=eq,
        referenced_logs={"log": [log_id]},
    )
    assert resp.status_code == 200, resp.text

    resp = await client.get(
        "/v0/logs",
        params={"project_name": project, "from_ids": str(log_id)},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    log = resp.json()["logs"][0]
    assert log["derived_entries"].get("sum_a") == 6


@pytest.mark.anyio
async def test_derived_creation_batched_counts_not_cumulative(
    client: AsyncClient,
):
    """
    Create logs in batches and immediately create derived logs for each batch's IDs.
    Verify each create-derived call reports only the count for that batch (not cumulative).
    """
    project_name = "test_derived_batched_counts"
    await _create_project(client, project_name, user=1)

    key = "text_embed"
    equation = "embed({log:text_content})"

    batch_sizes = [5, 10, 15]
    for batch_idx, size in enumerate(batch_sizes):
        # Create a batch of base logs
        entries = [
            {"text_content": f"batch-{batch_idx}-sample-{i}"} for i in range(size)
        ]
        resp = await client.post(
            "/v0/logs",
            json={"project_name": project_name, "entries": entries},
            headers=HEADERS,
        )
        assert resp.status_code == 200, resp.text
        log_event_ids = resp.json()["log_event_ids"]
        assert len(log_event_ids) == size

        # Create derived logs referencing only this batch's IDs
        derived_resp = await _create_derived_entry(
            client,
            project_name,
            key,
            equation,
            referenced_logs={"log": log_event_ids},
        )
        assert derived_resp.status_code == 200, derived_resp.text
        info_msg = derived_resp.json().get("info", "")
        assert (
            f"Created {size} derived logs" in info_msg
        ), f"Unexpected info message for batch {batch_idx}: {info_msg}"


@pytest.mark.anyio
async def test_create_derived_entry_with_filter_expr(
    client: AsyncClient,
):
    project_name = "test_project_filter"
    await _create_project(client, project_name, user=1)

    # Create base logs
    log_ids = []
    for i in range(5):
        response = await _create_log(client, project_name, entries={"score": i * 10})
        assert response.status_code == 200
        log_ids.append(response.json())

    # Create derived logs using filter_expr to select logs with score > 20
    referenced_logs = {"log1": {"filter_expr": "score > 20"}}
    key = "half_score"
    equation = "{log1:score}/2"
    response = await _create_derived_entry(
        client,
        project_name,
        key,
        equation,
        referenced_logs,
    )
    assert response.status_code == 200


@pytest.mark.anyio
async def test_update_derived_entry_with_referenced_logs(
    client: AsyncClient,
):
    project_name = "test_update_derived_refs"
    await _create_project(client, project_name)

    # 1. Create base logs with temperature values
    base_log_ids = []
    temps = [20.0, 25.0, 30.0, 35.0]  # Four base logs
    for temp in temps:
        resp = await client.post(
            "/v0/logs",
            json={"project_name": project_name, "entries": {"temperature": temp}},
            headers=HEADERS,
        )
        assert resp.status_code == 200
        base_log_ids.append(resp.json()["log_event_ids"][0])

    assert len(base_log_ids) == 4, "Expected to create 4 base logs"

    # 2. Create initial derived log using first two base logs
    initial_referenced_logs = {
        "t": [base_log_ids[0], base_log_ids[1]],
    }  # Only first two logs
    resp = await _create_derived_entry(
        client,
        project_name,
        key="temp_plus_10",
        equation="{t:temperature} + 10",
        referenced_logs=initial_referenced_logs,
    )
    assert resp.status_code == 200
    # Verify initial derived values
    resp = await client.get(f"/v0/logs?project_name={project_name}", headers=HEADERS)
    assert resp.status_code == 200
    logs = resp.json()["logs"]

    # Check initial derived values (only first two logs should have derived entries)
    for log in logs:
        if log["id"] in [base_log_ids[0], base_log_ids[1]]:
            assert "temp_plus_10" in log["derived_entries"]
            assert (
                log["derived_entries"]["temp_plus_10"]
                == log["entries"]["temperature"] + 10
            )
        else:
            assert "temp_plus_10" not in log["derived_entries"]

    # 3. Update derived log to use different base logs and modified equation
    new_referenced_logs = {
        "t": [base_log_ids[2], base_log_ids[3]],
    }  # Use last two logs instead
    resp = await client.put(
        "/v0/logs/derived",
        json={
            "project_name": project_name,
            "target_derived_logs": {"from_fields": "temp_plus_10"},
            "key": "temp_plus_10",
            "equation": "{t:temperature} + 20",  # Modified equation
            "referenced_logs": new_referenced_logs,
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200

    # 4. Verify the updates
    resp = await client.get(f"/v0/logs?project_name={project_name}", headers=HEADERS)
    assert resp.status_code == 200
    updated_logs = resp.json()["logs"]

    # Previous logs should no longer have derived entries
    for log in updated_logs:
        if log["id"] in [base_log_ids[0], base_log_ids[1]]:
            assert (
                "temp_plus_10" not in log["derived_entries"]
            ), f"Log {log['id']} should no longer have derived entry"
        elif log["id"] in [base_log_ids[2], base_log_ids[3]]:
            assert (
                "temp_plus_10" in log["derived_entries"]
            ), f"Log {log['id']} should now have derived entry"
            # Verify new equation is used (temp + 20 instead of temp + 10)
            assert (
                log["derived_entries"]["temp_plus_10"]
                == log["entries"]["temperature"] + 20
            )


@pytest.mark.anyio
async def test_update_derived_entry_with_filter(client: AsyncClient):
    project_name = "test_update_derived_filter"
    await _create_project(client, project_name)

    # 1) Create a few base logs
    base_log_ids = []
    temps = [20.0, 25.0, 30.0]
    for temp in temps:
        resp = await client.post(
            "/v0/logs",
            json={"project_name": project_name, "entries": {"temperature": temp}},
            headers=HEADERS,
        )
        assert resp.status_code == 200
        base_log_ids.append(resp.json()["log_event_ids"][0])

    # 2) Create first derived log: "temp_plus_10"
    resp = await _create_derived_entry(
        client,
        project_name,
        key="temp_plus_10",
        equation="{t:temperature} + 10",
        referenced_logs={"t": [base_log_ids[0], base_log_ids[1]]},
    )
    assert resp.status_code == 200

    # 3) Create second derived log: "temp_minus_5"
    resp = await _create_derived_entry(
        client,
        project_name,
        key="temp_minus_5",
        equation="{t:temperature} - 5",
        referenced_logs={"t": [base_log_ids[2]]},
    )
    assert resp.status_code == 200

    # Now we have 3 derived logs in total:
    #   2 of them with key="temp_plus_10"
    #   1 of them with key="temp_minus_5"

    # 4) Update ONLY the logs with key="temp_plus_10" => rename them to "temp_times_3"
    # Also update the referenced_logs to only use first two base logs
    resp = await client.put(
        "/v0/logs/derived",
        json={
            "project_name": project_name,
            "target_derived_logs": {"from_fields": "temp_plus_10"},
            "key": "temp_plus_10",
            "equation": "{t:temperature} * 3",
            "referenced_logs": {"t": [base_log_ids[0], base_log_ids[1]]},
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200
    updated_info = resp.json()
    assert "Updated" in updated_info["info"]

    # 5) Check final state: only the "temp_plus_10" logs should be changed
    resp = await client.get(f"/v0/logs?project_name={project_name}", headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()

    # We'll gather how many logs have key="temp_times_3" vs. "temp_minus_5"
    num_plus_10 = 0
    num_minus_5 = 0

    for log_obj in data["logs"]:
        derived_entries = log_obj.get("derived_entries", {})
        # Check if they have "temp_times_3"
        if "temp_plus_10" in derived_entries:
            num_plus_10 += 1
            # verify correctness of the computed value
            base_temp = log_obj["entries"].get("temperature")
            assert derived_entries["temp_plus_10"] == base_temp * 3
        if "temp_minus_5" in derived_entries:
            num_minus_5 += 1
            # verify correctness
            base_temp = log_obj["entries"].get("temperature")
            assert derived_entries["temp_minus_5"] == base_temp - 5

    # We expect 2 logs with "temp_times_3" (the old plus_10 ones)
    assert num_plus_10 == 2
    # We expect 1 log with "temp_minus_5"
    assert num_minus_5 == 1


@pytest.mark.anyio
async def test_get_logs_including_derived(client: AsyncClient):
    project_name = "test_derived_logs"
    user_id = 1

    # 1) Create a new project
    await _create_project(client, project_name, user=1)

    # 2) Populate base logs
    await _create_several_logs(client, project_name)  ##

    # 3) Create derived logs referencing some subsets
    base_log_ids1 = [1, 2, 3, 4]
    derived_conf1 = {
        "key": "dl1",
        "equation": "{log1:_/temperature} + 10",
        "referenced_logs": {"log1": base_log_ids1},
    }
    resp = await _create_derived_entry(
        client,
        project_name,
        derived_conf1["key"],
        derived_conf1["equation"],
        derived_conf1["referenced_logs"],
        user=user_id,
    )
    assert resp.status_code == 200, resp.json()

    base_log_ids2 = [1, 2, 3, 4, 5, 6]
    derived_conf2 = {
        "key": "dl2",
        "equation": "'lava' in {log1:_/description}",
        "referenced_logs": {"log1": base_log_ids2},
    }
    resp = await _create_derived_entry(
        client,
        project_name,
        derived_conf2["key"],
        derived_conf2["equation"],
        derived_conf2["referenced_logs"],
        user=user_id,
    )
    assert resp.status_code == 200, resp.json()

    # Third derived log checking if _/safe is True
    base_log_ids3 = [1, 2, 3, 4]
    derived_conf3 = {
        "key": "dl3",
        "equation": "{log1:_/safe} is True",
        "referenced_logs": {"log1": base_log_ids3},
    }
    resp = await _create_derived_entry(
        client,
        project_name,
        derived_conf3["key"],
        derived_conf3["equation"],
        derived_conf3["referenced_logs"],
        user=user_id,
    )
    assert resp.status_code == 200, resp.json()

    # 4) Test retrieving logs *without* any filtering or sorting
    resp = await client.get(
        "/v0/logs",
        params={"project_name": project_name},
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.json()
    data = resp.json()
    all_logs = data["logs"]
    count = data["count"]

    assert count == 7
    found_derived_for_first_event = False
    for entry in all_logs:
        derived_entries = entry.get("derived_entries")
        if derived_entries and "dl1" in derived_entries:
            found_derived_for_first_event = True
            break

    assert (
        found_derived_for_first_event
    ), "Expected to find at least one event with dl1 in derived_entries"

    # 5) Test column context
    resp = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "column_context": "_/",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200
    data_context = resp.json()
    logs_context = data_context["logs"]
    for log_obj in logs_context:
        # With column_context="_/", only fields that originally started with "_/" should be returned
        # After context stripping, they should be simple names like "temperature", "description", etc.
        for k in log_obj["entries"]:
            # These are fields from logs_for_various which all start with "_/" prefix
            # After context stripping, they become simple names
            pass  # Just verify the response is parseable

    # 6) Test a filter_expr,
    filter_expr = "_/temperature > 100"
    resp = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "filter_expr": filter_expr,
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200
    data_filtered = resp.json()
    logs_filtered = data_filtered["logs"]

    for log_obj in logs_filtered:
        # "dl1" should be in derived_entries
        dval = log_obj["derived_entries"].get("dl1", None)
        assert (
            dval is not None
        ), f"Expected derived dl1 in filter dl1>100, but not found: {log_obj}"
        assert dval > 500, f"dl1 is not > 500 for log {log_obj}"

    # 7) Test exclude_ids or from_ids
    resp = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "exclude_ids": "3",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200
    data_exclude = resp.json()
    logs_exclude = data_exclude["logs"]
    for log_obj in logs_exclude:
        # none should have id=3
        assert log_obj["id"] != 3, "We wanted to exclude log_event_id=3"

    # 8) Test sorting
    sorting_param = json.dumps({"_/temperature": "ascending"})
    resp = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "sorting": sorting_param,
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200
    data_sorted = resp.json()
    logs_sorted = data_sorted["logs"]

    # check that the events are in ascending order of their _/temperature
    last_temp = float("-inf")
    for log_obj in logs_sorted:
        temperature = log_obj["entries"].get("_/temperature")
        if temperature is not None:
            # We expect temperature >= last_temp each time
            assert (
                temperature >= last_temp
            ), f"Sorting by temperature ascending failed: {temperature} < {last_temp}"
            last_temp = temperature


@pytest.mark.anyio
async def test_update_logs_and_derived_logs_are_updated(
    client: AsyncClient,
):
    project_name = "test_project_update_logs"
    await _create_project(client, project_name, user=1)

    # Create base logs
    base_log_ids = []
    for i in range(2):
        response = await _create_log(client, project_name, entries={"a": i + 1})
        assert response.status_code == 200
        base_log_ids.append(response.json()["log_event_ids"][0])

    # Create derived logs
    key = "add_one"
    equation = "{log0:a}+1"
    referenced_logs = {"log0": base_log_ids}
    derived_response = await _create_derived_entry(
        client,
        project_name,
        key,
        equation,
        referenced_logs,
    )
    assert derived_response.status_code == 200

    # Update base logs
    update_payload = {
        "logs": base_log_ids,
        "entries": [{"a": 10}, {"a": 20}],
        "overwrite": True,
    }
    response = await client.put(
        "/v0/logs",
        json=update_payload,
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert response.json()["info"] == "Logs updated successfully!"

    response = await client.get(
        "/v0/logs",
        params={"project_name": project_name},
        headers=HEADERS,
    )
    assert response.status_code == 200
    data = response.json()
    assert len(data["logs"]) == 2
    # Verify base logs are updated
    assert data["logs"][0]["entries"]["a"] == 20
    assert data["logs"][1]["entries"]["a"] == 10
    # Verify derived logs are updated
    assert data["logs"][0]["derived_entries"]["add_one"] == 21
    assert data["logs"][1]["derived_entries"]["add_one"] == 11


@pytest.mark.anyio
async def test_delete_derived_logs(client: AsyncClient):
    project_name = "test_delete_derived"
    await _create_project(client, project_name)

    # Create base logs
    log_ids = []
    for i in range(3):
        response = await _create_log(client, project_name, entries={"a": i * 10})
        assert response.status_code == 200
        log_ids.append(response.json()["log_event_ids"][0])

    # Create first derived log
    key1 = "derived1"
    equation1 = "{log1:a}*2"
    referenced_logs1 = {"log1": log_ids}
    response = await _create_derived_entry(
        client,
        project_name,
        key1,
        equation1,
        referenced_logs1,
    )
    assert response.status_code == 200

    # Create second derived log
    key2 = "derived2"
    equation2 = "{log1:a}+5"
    referenced_logs2 = {"log1": log_ids}
    response = await _create_derived_entry(
        client,
        project_name,
        key2,
        equation2,
        referenced_logs2,
    )
    assert response.status_code == 200

    # Delete only the first derived log using unified endpoint
    ids_and_fields = [(id_, key1) for id_ in log_ids]
    response = await _delete_logs(
        client,
        ids_and_fields,
        project_name=project_name,
        source_type="derived",
    )
    assert response.status_code == 200
    assert "Logs and fields deleted successfully" in response.json()["info"]

    # Verify first derived log is deleted but second remains
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    logs = response.json()["logs"]

    for log in logs:
        assert "derived1" not in log["derived_entries"]
        if log["id"] in log_ids:  # Only check base logs that had derived entries
            assert "derived2" in log["derived_entries"]

    # Delete second derived log using unified endpoint
    ids_and_fields = [(id_, key2) for id_ in log_ids]
    response = await _delete_logs(
        client,
        ids_and_fields,
        project_name=project_name,
        source_type="derived",
    )
    assert response.status_code == 200
    assert "Logs and fields deleted successfully" in response.json()["info"]

    # Verify all derived logs are deleted
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    logs = response.json()["logs"]
    for log in logs:
        assert len(log["derived_entries"]) == 0


@pytest.mark.anyio
async def test_derived_entry_datetime_arithmetic(client: AsyncClient):
    """
    Test datetime, time, and timedelta arithmetic in derived log entries.

    This test verifies that derived entries can:
    1. Perform basic datetime arithmetic (add/subtract time periods)
    2. Calculate time differences between timestamps
    3. Extract date and time components
    4. Handle timezone-aware timestamps
    5. Calculate midpoints between timestamps
    6. Work with fractional seconds
    7. Perform complex chained datetime operations
    """
    project_name = "test_derived_datetime_arithmetic"
    await _create_project(client, project_name)

    # Create logs with various datetime values
    logs_data = [
        # Basic timestamp for simple operations
        {
            "entries": {
                "dt/timestamp": "2023-06-15T14:30:45+00:00",
                "dt/name": "basic_timestamp",
            },
        },
        # Two timestamps for interval calculation
        {
            "entries": {
                "dt/start": "2023-06-15T10:00:00+00:00",
                "dt/end": "2023-06-15T16:00:00+00:00",
                "dt/name": "interval_calculation",
            },
        },
        # Timestamps with fractional seconds
        {
            "entries": {
                "dt/precise_ts": "2023-06-15T14:30:45.123456+00:00",
                "dt/name": "fractional_seconds",
            },
        },
        # Timestamps in different timezones
        {
            "entries": {
                "dt/utc_time": "2023-06-15T12:00:00+00:00",
                "dt/est_time": "2023-06-15T12:00:00-05:00",
                "dt/name": "timezone_aware",
            },
        },
        # Month boundary timestamps
        {
            "entries": {
                "dt/month_end": "2023-06-30T23:59:59.999+00:00",
                "dt/next_month": "2023-07-01T00:00:00.001+00:00",
                "dt/name": "month_boundary",
            },
        },
    ]

    # Create the logs and store their IDs
    log_ids = []
    for log_data in logs_data:
        response = await client.post(
            "/v0/logs",
            json={"project_name": project_name, "entries": log_data["entries"]},
            headers=HEADERS,
        )
        assert response.status_code == 200, response.text
        log_ids.append(response.json()["log_event_ids"][0])

    # 1. Test adding time to a timestamp
    response = await _create_derived_entry(
        client,
        project_name,
        key="timestamp_plus_1hour",
        equation="{log:dt/timestamp} + 'PT1H'",
        referenced_logs={"log": [log_ids[0]]},
    )
    assert response.status_code == 200, response.text

    # 2. Test subtracting time from a timestamp
    response = await _create_derived_entry(
        client,
        project_name,
        key="timestamp_minus_30min",
        equation="{log:dt/timestamp} - 'PT30M'",
        referenced_logs={"log": [log_ids[0]]},
    )
    assert response.status_code == 200, response.text

    # 3. Test calculating duration between timestamps
    response = await _create_derived_entry(
        client,
        project_name,
        key="duration_hours",
        equation="({log:dt/end} - {log:dt/start}) == 'PT6H'",
        referenced_logs={"log": [log_ids[1]]},
    )
    assert response.status_code == 200, response.text

    # 4. Test calculating midpoint between timestamps
    response = await _create_derived_entry(
        client,
        project_name,
        key="midpoint_timestamp",
        equation="{log:dt/start} + (({log:dt/end} - {log:dt/start}) / 2)",
        referenced_logs={"log": [log_ids[1]]},
    )
    assert response.status_code == 200, response.text

    # 5. Test extracting date component
    response = await _create_derived_entry(
        client,
        project_name,
        key="extracted_date",
        equation="date({log:dt/timestamp})",
        referenced_logs={"log": [log_ids[0]]},
    )
    assert response.status_code == 200, response.text

    # 6. Test extracting time component
    response = await _create_derived_entry(
        client,
        project_name,
        key="extracted_time",
        equation="time({log:dt/timestamp})",
        referenced_logs={"log": [log_ids[0]]},
    )
    assert response.status_code == 200, response.text

    # 7. Test timezone-aware comparison
    response = await _create_derived_entry(
        client,
        project_name,
        key="timezone_difference",
        equation="{log:dt/utc_time} != {log:dt/est_time}",
        referenced_logs={"log": [log_ids[3]]},
    )
    assert response.status_code == 200, response.text

    # 8. Test timezone-aware duration calculation
    response = await _create_derived_entry(
        client,
        project_name,
        key="timezone_duration",
        equation="{log:dt/est_time} - {log:dt/utc_time}",
        referenced_logs={"log": [log_ids[3]]},
    )
    assert response.status_code == 200, response.text

    # 9. Test fractional seconds handling
    response = await _create_derived_entry(
        client,
        project_name,
        key="precise_plus_500ms",
        equation="{log:dt/precise_ts} + 'PT0.5S'",
        referenced_logs={"log": [log_ids[2]]},
    )
    assert response.status_code == 200, response.text

    # 10. Test month boundary duration
    response = await _create_derived_entry(
        client,
        project_name,
        key="month_boundary_diff",
        equation="{log:dt/next_month} - {log:dt/month_end}",
        referenced_logs={"log": [log_ids[4]]},
    )
    assert response.status_code == 200, response.text

    # 11. Test complex chained operation
    response = await _create_derived_entry(
        client,
        project_name,
        key="complex_operation",
        equation="date({log:dt/timestamp} + 'P1D') != date({log:dt/timestamp})",
        referenced_logs={"log": [log_ids[0]]},
    )
    assert response.status_code == 200, response.text

    # Verify the derived entries
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    logs = response.json()["logs"]

    # Check each log for its derived entries
    for log in logs:
        if log["entries"].get("dt/name") == "basic_timestamp":
            # Check timestamp arithmetic
            assert "timestamp_plus_1hour" in log["derived_entries"]
            assert (
                log["derived_entries"]["timestamp_plus_1hour"]
                == "2023-06-15T15:30:45+00:00"
            )

            assert "timestamp_minus_30min" in log["derived_entries"]
            assert (
                log["derived_entries"]["timestamp_minus_30min"]
                == "2023-06-15T14:00:45+00:00"
            )

            # Check date/time extraction
            assert "extracted_date" in log["derived_entries"]
            assert log["derived_entries"]["extracted_date"] == "2023-06-15"

            assert "extracted_time" in log["derived_entries"]
            assert log["derived_entries"]["extracted_time"] == "14:30:45.000000"

            # Check complex operation
            assert "complex_operation" in log["derived_entries"]
            assert log["derived_entries"]["complex_operation"] is True

        elif log["entries"].get("dt/name") == "interval_calculation":
            # Check duration calculation
            assert "duration_hours" in log["derived_entries"]
            assert log["derived_entries"]["duration_hours"] is True

            # Check midpoint calculation
            assert "midpoint_timestamp" in log["derived_entries"]
            assert (
                log["derived_entries"]["midpoint_timestamp"]
                == "2023-06-15T13:00:00+00:00"
            )

        elif log["entries"].get("dt/name") == "fractional_seconds":
            # Check fractional seconds handling
            assert "precise_plus_500ms" in log["derived_entries"]
            assert (
                log["derived_entries"]["precise_plus_500ms"]
                == "2023-06-15T14:30:45.623456+00:00"
            )

        elif log["entries"].get("dt/name") == "timezone_aware":
            # Check timezone-aware comparison
            assert "timezone_difference" in log["derived_entries"]
            assert log["derived_entries"]["timezone_difference"] is True

            # Check timezone-aware duration
            assert "timezone_duration" in log["derived_entries"]
            assert log["derived_entries"]["timezone_duration"] == "PT0S"

        elif log["entries"].get("dt/name") == "month_boundary":
            # Check month boundary duration
            assert "month_boundary_diff" in log["derived_entries"]
            assert log["derived_entries"]["month_boundary_diff"] == "PT0.002S"

    # Test updating a derived entry with datetime arithmetic
    response = await client.put(
        "/v0/logs/derived",
        json={
            "project_name": project_name,
            "target_derived_logs": {"from_fields": "timestamp_plus_1hour"},
            "equation": "{log:dt/timestamp} + 'PT2H'",  # Change from +1h to +2h
            "referenced_logs": {"log": [log_ids[0]]},
            "key": "timestamp_plus_1hour",
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text

    # Verify the update
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    logs = response.json()["logs"]

    for log in logs:
        if log["entries"].get("dt/name") == "basic_timestamp":
            assert "timestamp_plus_1hour" in log["derived_entries"]
            assert (
                log["derived_entries"]["timestamp_plus_1hour"]
                == "2023-06-15T16:30:45+00:00"
            )


@pytest.mark.anyio
async def test_active_derived_logs_processing(client: AsyncClient):
    """
    Test the admin endpoint for processing active derived logs.
    This test verifies that after calling the admin endpoint, the new log gets the derived entry
    """
    import os

    # Set up project and create base logs
    project_name = "test_admin_derived_processing"
    await _create_project(client, project_name, user=1)
    # Create base logs with different scores
    log_ids = []
    for score in [10, 30, 50, 70, 90, 100]:
        response = await _create_log(
            client,
            project_name,
            entries={"score": score, "average_score": score * 0.5},
            params={},
        )
        assert response.status_code == 200
        log_ids.append(response.json()["log_event_ids"][0])

    # Create a derived log with filter_expr to select logs with score > 40
    key = "high_score_flag"
    equation = "{log:score} ** {log:average_score}"
    referenced_logs = {"log": {"filter_expr": ""}}

    response = await _create_derived_entry(
        client,
        project_name,
        key,
        equation,
        referenced_logs,
    )
    assert response.status_code == 200

    # Verify only logs with score > 40 have the derived entry
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    logs = response.json()["logs"]

    for log in logs:
        assert "high_score_flag" in log["derived_entries"]

    # Create a new log that should match the filter but doesn't have the derived entry yet
    new_log_score = 60
    response = await _create_log(
        client,
        project_name,
        entries={"score": new_log_score, "average_score": new_log_score * 0.5},
        params={},
    )
    assert response.status_code == 200
    new_log_id = response.json()["log_event_ids"][0]

    # Verify the new log doesn't have the derived entry yet
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        params={"from_ids": new_log_id},
        headers=HEADERS,
    )
    assert response.status_code == 200
    new_log = response.json()["logs"][0]
    assert "high_score_flag" not in new_log["derived_entries"]

    # Call the admin endpoint to process active derived logs
    admin_headers = HEADERS.copy()
    admin_headers["Authorization"] = f"Bearer {os.environ['ORCHESTRA_ADMIN_KEY']}"

    response = await client.post(
        "/v0/admin/update_active_derived_logs",
        headers=admin_headers,
    )
    assert response.status_code == 200

    # Verify the new log now has the derived entry
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        params={"from_ids": new_log_id},
        headers=HEADERS,
    )
    assert response.status_code == 200
    updated_log = response.json()["logs"][0]
    assert "high_score_flag" in updated_log["derived_entries"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "test_case",
    [
        # 1)  list‑comp with filter
        {
            "equation": "[x*2 for x in {log:nums} if x > 1]",
            "referenced_logs": {"log": {"filter_expr": ""}},
            "key": "derived_0",
            "expected": [4, 6],
        },
        # 2)  dict‑comp with filter
        {
            "equation": "{k: v for k, v in zip({log:keys}, {log:vals}) if v > 150}",
            "referenced_logs": {"log": {"filter_expr": ""}},
            "key": "derived_1",
            "expected": {"b": 200, "c": 300},
        },
        # 3)  list‑comp with conditional
        {
            "equation": "[x if f else -x for x, f in zip({log:vals}, {log:flags})]",
            "referenced_logs": {"log": {"filter_expr": ""}},
            "key": "derived_2",
            "expected": [100, -200, 300],
        },
        # 4)  list‑comp with nested list‑comp
        {
            "equation": "[[y*10 for y in x] for x in {log:nested}]",
            "referenced_logs": {"log": {"filter_expr": ""}},
            "key": "derived_3",
            "expected": [[10, 20], [30, 40], [50, 60]],
        },
        # 5)  list‑comp with conditional and outer filter
        {
            "equation": "[k+str(v) for k, v in zip({log:keys}, {log:vals}) if v != 200]",
            "referenced_logs": {"log": {"filter_expr": ""}},
            "key": "derived_4",
            "expected": ["a100", "c300"],
        },
        # 6)  list‑comp with zip
        {
            "equation": "zip({log:nums}, {log:alts}, {log:flags})",
            "referenced_logs": {"log": {"filter_expr": ""}},
            "key": "derived_5",
            "expected": [[1, 10, True], [2, 20, False], [3, 30, True]],
        },
        # 7)  nested dict‑comp inside a list‑comp
        {
            "equation": "[{k:str(v) for k,v in d.items()} for d in {log:dicts}]",
            "referenced_logs": {"log": {"filter_expr": ""}},
            "key": "derived_6",
            "expected": [
                {"x": "1", "y": "2"},
                {"x": "3", "y": "4"},
            ],
        },
        # 8)  dict‑comp with nested list‑comp on RHS
        {
            "equation": "{k: [v*i for i in {log:multipliers}] "
            " for k, v in zip({log:keys}, {log:nums})}",
            "referenced_logs": {"log": {"filter_expr": ""}},
            "key": "derived_7",
            "expected": {
                "a": [1, 2, 3],
                "b": [2, 4, 6],
                "c": [3, 6, 9],
            },
        },
        # 9)  nested conditional inside nested comprehension
        {
            "equation": "[[y if y%2==0 else -y for y in x] " " for x in {log:nested}]",
            "referenced_logs": {"log": {"filter_expr": ""}},
            "key": "derived_8",
            "expected": [[-1, 2], [-3, 4], [-5, 6]],
        },
        # 10)  dict‑comp with its own if‑expr and an outer filter
        {
            "equation": "{k: (v if v<200 else None) "
            " for k,v in zip({log:keys}, {log:vals}) if k!='c'}",
            "referenced_logs": {"log": {"filter_expr": ""}},
            "key": "derived_9",
            "expected": {"a": 100, "b": None},
        },
    ],
)
async def test_advanced_comprehensions_and_conditionals(
    client: AsyncClient,
    test_case,
):
    project = "advanced-test"
    await _create_project(client, project)

    entries = []
    for i in range(3):
        entries.append(
            {
                "nums": [1, 2, 3],
                "alts": [10, 20, 30],
                "keys": ["a", "b", "c"],
                "vals": [100, 200, 300],
                "flags": [True, False, True],
                "nested": [[1, 2], [3, 4], [5, 6]],
                "dicts": [{"x": 1, "y": 2}, {"x": 3, "y": 4}],
                "multipliers": [1, 2, 3],
            },
        )
    await _create_log(client, project, entries=entries)

    field = test_case["key"]
    response = await _create_derived_entry(
        client,
        project,
        equation=test_case["equation"],
        key=field,
        referenced_logs=test_case["referenced_logs"],
    )
    assert (
        response.status_code == 200
    ), f"Failed to create derived entry: {response.text}"

    response = await client.get(
        f"/v0/logs?project_name={project}&filter_expr={field} is not None",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert result["logs"][0]["derived_entries"][field] == test_case["expected"]


@pytest.mark.anyio
async def test_create_static_entries_with_flag(client: AsyncClient):
    """
    Test creating static entries with derived=false flag.

    This test verifies that when the derived=false flag is passed to the /logs/derived endpoint,
    the computed values are stored directly in the base logs' entries rather than in derived_entries.
    """
    # 1. Create a project
    project_name = "test_static_entries"
    await _create_project(client, project_name, user=1)

    # 2. Create base logs
    log_ids = []
    for i in range(3):
        response = await _create_log(client, project_name, entries={"value": i * 10})
        assert response.status_code == 200
        log_ids.append(response.json()["log_event_ids"][0])

    # 3. Call /v0/logs/derived with derived=false
    key = "doubled_value"
    equation = "{log:value} * 2"
    referenced_logs = {"log": log_ids}

    # Use direct client.post to include the derived=false flag
    response = await client.post(
        "/v0/logs/derived",
        json={
            "project_name": project_name,
            "key": key,
            "equation": equation,
            "referenced_logs": referenced_logs,
            "derived": False,  # This is the key flag we're testing
        },
        headers=HEADERS,
    )

    assert response.status_code == 200

    # 4. Fetch logs and verify the new key is in entries, not in derived_entries
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    logs = response.json()["logs"]

    # Verify each log has the computed value in entries, not in derived_entries
    for i, log in enumerate(logs):
        # The value should be in regular entries
        assert key in log["entries"]
        assert log["entries"][key] == log["entries"]["value"] * 2

        # The value should NOT be in derived_entries
        assert key not in log["derived_entries"]


@pytest.mark.anyio
async def test_division_by_zero_safeguarding_all_operators(
    client: AsyncClient,
):
    """
    Test that division by zero is properly safeguarded for all arithmetic operators
    that involve division: regular division (/), modulo (%), and floor division (//).

    This test verifies that when the denominator contains zero values, the operations
    return NULL instead of throwing division by zero errors.
    """
    project_name = "test_division_by_zero_safeguarding"
    await _create_project(client, project_name, user=1)

    # Create logs with various numerator and denominator values, including zeros
    log_entries = [
        # Case 1: Normal division (non-zero denominator)
        {"numerator": 10, "denominator": 2, "case": "normal"},
        # Case 2: Division by zero
        {"numerator": 15, "denominator": 0, "case": "div_by_zero"},
        # Case 3: Zero divided by non-zero (should work normally)
        {"numerator": 0, "denominator": 5, "case": "zero_numerator"},
        # Case 4: Another normal case
        {"numerator": 21, "denominator": 7, "case": "normal_2"},
        # Case 5: Another division by zero case
        {"numerator": 8, "denominator": 0, "case": "div_by_zero_2"},
        # Case 6: Larger numbers for testing
        {"numerator": 100, "denominator": 3, "case": "large_normal"},
    ]

    # Create all the logs
    log_ids = []
    for entry in log_entries:
        response = await _create_log(client, project_name, entries=entry)
        assert response.status_code == 200
        log_ids.append(response.json()["log_event_ids"][0])

    # Test 1: Regular division (/) with zero safeguarding
    division_key = "regular_division"
    division_equation = "{log:numerator} / {log:denominator}"
    referenced_logs = {"log": log_ids}

    response = await _create_derived_entry(
        client,
        project_name,
        division_key,
        division_equation,
        referenced_logs,
    )
    assert response.status_code == 200, f"Regular division failed: {response.text}"

    # Test 2: Modulo (%) with zero safeguarding
    modulo_key = "modulo_operation"
    modulo_equation = "{log:numerator} % {log:denominator}"

    response = await _create_derived_entry(
        client,
        project_name,
        modulo_key,
        modulo_equation,
        referenced_logs,
    )
    assert response.status_code == 200, f"Modulo operation failed: {response.text}"

    # Test 3: Floor division (//) with zero safeguarding
    floor_div_key = "floor_division"
    floor_div_equation = "{log:numerator} // {log:denominator}"

    response = await _create_derived_entry(
        client,
        project_name,
        floor_div_key,
        floor_div_equation,
        referenced_logs,
    )
    assert response.status_code == 200, f"Floor division failed: {response.text}"

    # Verify the results
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    logs = response.json()["logs"]

    # Check each log's derived entries
    for log in logs:
        case_type = log["entries"]["case"]
        numerator = log["entries"]["numerator"]
        denominator = log["entries"]["denominator"]

        if denominator == 0:
            # Division by zero cases should result in NULL values (keys exist but values are None)
            assert (
                division_key in log["derived_entries"]
            ), f"Division key should exist for case {case_type}"
            assert (
                log["derived_entries"][division_key] is None
            ), f"Division by zero should result in None for case {case_type}"

            assert (
                modulo_key in log["derived_entries"]
            ), f"Modulo key should exist for case {case_type}"
            assert (
                log["derived_entries"][modulo_key] is None
            ), f"Modulo by zero should result in None for case {case_type}"

            assert (
                floor_div_key in log["derived_entries"]
            ), f"Floor division key should exist for case {case_type}"
            assert (
                log["derived_entries"][floor_div_key] is None
            ), f"Floor division by zero should result in None for case {case_type}"
        else:
            # Non-zero denominator cases should have valid results
            assert (
                division_key in log["derived_entries"]
            ), f"Regular division should have result for case {case_type}"
            assert (
                modulo_key in log["derived_entries"]
            ), f"Modulo should have result for case {case_type}"
            assert (
                floor_div_key in log["derived_entries"]
            ), f"Floor division should have result for case {case_type}"

            # Verify the values are not None and mathematically correct
            actual_division = log["derived_entries"][division_key]
            actual_modulo = log["derived_entries"][modulo_key]
            actual_floor_div = log["derived_entries"][floor_div_key]

            assert (
                actual_division is not None
            ), f"Division result should not be None for case {case_type}"
            assert (
                actual_modulo is not None
            ), f"Modulo result should not be None for case {case_type}"
            assert (
                actual_floor_div is not None
            ), f"Floor division result should not be None for case {case_type}"

            # Verify the mathematical correctness
            expected_division = numerator / denominator
            expected_modulo = numerator % denominator
            expected_floor_div = numerator // denominator

            assert (
                abs(actual_division - expected_division) < 0.0001
            ), f"Division result incorrect for case {case_type}: expected {expected_division}, got {actual_division}"
            assert (
                actual_modulo == expected_modulo
            ), f"Modulo result incorrect for case {case_type}: expected {expected_modulo}, got {actual_modulo}"
            assert (
                actual_floor_div == expected_floor_div
            ), f"Floor division result incorrect for case {case_type}: expected {expected_floor_div}, got {actual_floor_div}"

    # Additional test: Complex equation with conditional logic
    # This tests a more realistic scenario like the one mentioned in the user's description
    complex_key = "conditional_division"
    complex_equation = (
        "{log:numerator} / {log:denominator} if {log:denominator} != 0 else 999.0"
    )

    response = await _create_derived_entry(
        client,
        project_name,
        complex_key,
        complex_equation,
        referenced_logs,
    )
    assert (
        response.status_code == 200
    ), f"Complex conditional division failed: {response.text}"

    # Verify the complex equation results
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    logs = response.json()["logs"]

    for log in logs:
        case_type = log["entries"]["case"]
        numerator = log["entries"]["numerator"]
        denominator = log["entries"]["denominator"]

        assert (
            complex_key in log["derived_entries"]
        ), f"Complex conditional division should always have a result for case {case_type}"

        actual_result = log["derived_entries"][complex_key]

        if denominator == 0:
            # Should use the else value (999)
            assert (
                actual_result == 999
            ), f"Complex division should return 999 for zero denominator in case {case_type}, got {actual_result}"
        else:
            # Should compute the actual division
            expected_result = numerator / denominator
            assert (
                abs(actual_result - expected_result) < 0.0001
            ), f"Complex division result incorrect for case {case_type}: expected {expected_result}, got {actual_result}"


@pytest.mark.anyio
async def test_derived_embedding_and_filtering(client: AsyncClient):
    """
    Create an 'embedding' derived column once, then reuse it from filters.
    """
    project = "derived_embed_demo"
    await _create_project(client, project)

    # 1) Create base logs with 'cat','dog','chair' descriptions
    descriptions = ["a cute little cat", "a friendly dog", "a wooden chair"]
    log_ids = []
    for desc in descriptions:
        response = await _create_log(client, project, entries={"desc": desc})
        assert response.status_code == 200
        log_ids.append(response.json()["log_event_ids"][0])

    # 2) Add derived column: key="desc_vec", equation="embed(desc)"
    key = "desc_vec"
    equation = "embed({log:desc})"
    referenced_logs = {"log": log_ids}

    response = await _create_derived_entry(
        client,
        project,
        key,
        equation,
        referenced_logs,
    )
    assert response.status_code == 200, response.text

    # 3) Filter logs by similarity to 'cute little cat'
    filter_expr = "cosine(desc_vec, embed('cute little cat')) < 0.2"
    response = await client.get(
        "/v0/logs",
        params={
            "project_name": project,
            "filter_expr": filter_expr,
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # 4) Assert the first result is 'cat' and 'chair' is not returned
    logs = response.json()["logs"]
    assert len(logs) > 0, "Expected at least one log to match the filter"

    # First result should be the cat description (highest similarity)
    assert "cat" in logs[0]["entries"]["desc"]

    # Chair should not be in the results (similarity too low)
    chair_found = False
    for log in logs:
        if "chair" in log["entries"]["desc"]:
            chair_found = True
            break

    assert not chair_found, "Chair should not be returned due to low similarity"


@pytest.mark.anyio
@pytest.mark.xdist_group(name="gcs_serial")
async def test_derived_image_embedding_and_filtering(
    client: AsyncClient,
):
    """
    Test image embedding functionality:
    1. Create base logs with different images (cat, dog, car)
    2. Create derived column with embed_image() to generate embeddings
    3. Query using POST /logs/query with a query image
    4. Verify similarity-based filtering works correctly

    Note: Marked with xdist_group to run serially due to GCS eventual consistency issues.
    """
    project = "derived_image_embed_demo"
    context = "image_test"
    await _create_project(client, project)

    # 1) Create base logs with different images
    # Create log with cat image
    response_cat = await _create_image_log(
        client,
        project,
        context,
        "cat.png",
        additional_entries={"label": "cat"},
        image_col_name="screenshot",
    )
    assert response_cat.status_code == 200, response_cat.text
    cat_log_id = response_cat.json()["log_event_ids"][0]

    # Create log with dog image
    response_dog = await _create_image_log(
        client,
        project,
        context,
        "dog.png",
        additional_entries={"label": "dog"},
        image_col_name="screenshot",
    )
    assert response_dog.status_code == 200, response_dog.text
    dog_log_id = response_dog.json()["log_event_ids"][0]

    # Create log with car image
    response_car = await _create_image_log(
        client,
        project,
        context,
        "car.png",
        additional_entries={"label": "car"},
        image_col_name="screenshot",
    )
    assert response_car.status_code == 200, response_car.text
    car_log_id = response_car.json()["log_event_ids"][0]

    log_ids = [cat_log_id, dog_log_id, car_log_id]

    # Wait for GCS images to become available before computing embeddings
    await wait_for_gcs_images(client, project, context, image_col_name="screenshot")

    # 2) Create derived column with embed_image() to generate embeddings
    key = "screenshot_embedding"
    equation = "embed_image({log:screenshot})"
    referenced_logs = {"log": log_ids}

    response = await _create_derived_entry(
        client,
        project,
        key,
        equation,
        referenced_logs,
        context=context,
    )
    assert response.status_code == 200, response.text
    response_data = response.json()
    assert "Created" in response_data["info"]
    assert "3 derived logs" in response_data["info"]

    # 3) Verify the embeddings were created by fetching logs
    fetch_response = await client.get(
        "/v0/logs",
        params={
            "project_name": project,
            "context": context,
            "from_fields": "screenshot_embedding",
        },
        headers=HEADERS,
    )
    assert fetch_response.status_code == 200
    logs = fetch_response.json()["logs"]
    assert len(logs) == 3, "Should have 3 logs with embeddings"

    # Verify that embeddings exist (they should be stored in the Embedding table)
    # Each log should have the derived entry
    for log in logs:
        assert "screenshot_embedding" in log["derived_entries"]

    # 4) Test POST /logs/query with image similarity
    # Read the cat image again to use as query
    import os

    full_img_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.realpath(__file__))),
        "sample_datasets",
        "cat_2.png",  # Use a different cat image for similarity test
    )

    import cv2

    success, buffer = cv2.imencode(".png", cv2.imread(full_img_path))
    assert success, f"Failed to encode query image at {full_img_path}"
    query_img_b64 = base64.b64encode(buffer).decode("utf-8")

    # Construct filter and sorting expressions with embed_image()
    # The same expression is used for both filtering and sorting
    similarity_expr = f"cosine(screenshot_embedding, embed_image('data:image/png;base64,{query_img_b64}'))"
    filter_expr = f"{similarity_expr} < 0.35"

    # Sort by cosine distance (ascending = most similar first)
    sorting = json.dumps({similarity_expr: "ascending"})

    # Query using POST /logs/query (simplified - just pass filter_expr and sorting like GET)
    # POST allows large base64 strings that would exceed URL limits in GET
    query_response = await client.post(
        "/v0/logs/query",
        json={
            "project_name": project,
            "context": context,
            "filter_expr": filter_expr,
            "sorting": sorting,  # Sort by similarity
            "limit": 10,
        },
        headers=HEADERS,
    )
    assert query_response.status_code == 200, query_response.text
    query_logs = query_response.json()["logs"]

    # 5) Verify results - cat images should be more similar than car
    assert len(query_logs) > 0, "Expected at least one log to match the image query"

    # The first result should be a cat (highest similarity)
    first_result = query_logs[0]
    assert (
        first_result["entries"]["label"] == "cat"
    ), f"Expected first result to be 'cat', got '{first_result['entries']['label']}'"

    # Verify that car is less similar (might not be in results at all with threshold)
    car_found = False
    for log in query_logs:
        if log["entries"]["label"] == "car":
            car_found = True
            break

    # Car should either not be found or be last in the results
    if car_found:
        # If car is found, it should not be the first result
        assert (
            query_logs[0]["entries"]["label"] != "car"
        ), "Car should not be the most similar image to a cat query"


@pytest.mark.anyio
async def test_create_derived_entry_with_partial_null_values(
    client: AsyncClient,
):
    """
    Test creating derived entries where some logs have null/non-existent values
    for the referenced field, but not all logs are null.

    This verifies that the derived entry creation succeeds even when some
    referenced values are missing or null.
    """
    project_name = "test_partial_null_derived"
    await _create_project(client, project_name, user=1)

    # Create base logs with mixed presence of the target field
    log_ids = []

    # Log 0: Has the field but with null value
    response = await _create_log(
        client,
        project_name,
        entries={"temperature": None, "location": "outdoor"},
    )
    assert response.status_code == 200
    log_ids.append(response.json()["log_event_ids"][0])

    # Log 1: Has the field with a valid value
    response = await _create_log(
        client,
        project_name,
        entries={"temperature": 25.0, "location": "indoor"},
    )
    assert response.status_code == 200
    log_ids.append(response.json()["log_event_ids"][0])

    # Log 2: Has the field but with null value
    response = await _create_log(
        client,
        project_name,
        entries={"temperature": None, "location": "outdoor"},
    )
    assert response.status_code == 200
    log_ids.append(response.json()["log_event_ids"][0])

    # Log 3: Missing the field entirely
    response = await _create_log(client, project_name, entries={"location": "basement"})
    assert response.status_code == 200
    log_ids.append(response.json()["log_event_ids"][0])

    # Log 4: Has the field with another valid value
    response = await _create_log(
        client,
        project_name,
        entries={"temperature": 30.0, "location": "attic"},
    )
    assert response.status_code == 200
    log_ids.append(response.json()["log_event_ids"][0])

    # Log 5: Has the field with zero value (should not be treated as null)
    response = await _create_log(
        client,
        project_name,
        entries={"temperature": 0.0, "location": "freezer"},
    )
    assert response.status_code == 200
    log_ids.append(response.json()["log_event_ids"][0])

    # Log 6: Missing the field entirely again
    response = await _create_log(
        client,
        project_name,
        entries={"location": "living room"},
    )
    assert response.status_code == 200
    log_ids.append(response.json()["log_event_ids"][0])

    # Log 7: Has the field but with null value again
    response = await _create_log(
        client,
        project_name,
        entries={"temperature": None, "location": "basement"},
    )
    assert response.status_code == 200
    log_ids.append(response.json()["log_event_ids"][0])

    # Create derived entry that references the temperature field
    # This should succeed even though some logs don't have temperature values
    key = "temp_celsius_to_fahrenheit"
    equation = "{log:temperature} * 9 / 5 + 32"
    referenced_logs = {"log": log_ids}

    response = await _create_derived_entry(
        client,
        project_name,
        key,
        equation,
        referenced_logs,
    )

    # Assert that the operation succeeded
    assert (
        response.status_code == 200
    ), f"Expected 200 but got {response.status_code}: {response.text}"

    # Verify the derived field is not NoneType
    response = await client.get(
        f"/v0/logs/fields?project_name={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    fields = response.json()
    assert key in fields
    assert fields[key]["data_type"] == "float"

    # Verify the derived entries were created correctly
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200


@pytest.mark.anyio
async def test_create_static_entries_with_correct_id_alignment(
    client: AsyncClient,
):
    """
    Verifies that create_from_logs with derived=False correctly maps
    computed values to the right source logs, preventing ID misalignment.
    """
    project_name = "test_static_id_alignment"
    await _create_project(client, project_name, user=1)

    # 1. Create several logs with distinct values
    log_ids = []
    for i in range(5):
        # Values are 10, 20, 30, 40, 50
        response = await _create_log(
            client,
            project_name,
            entries={"value": (i + 1) * 10},
        )
        assert response.status_code == 200
        log_ids.append(response.json()["log_event_ids"][0])

    # 2. Use derived=False to create a new static entry on all logs
    key = "value_plus_100"
    equation = "{log:value} + 100"
    referenced_logs = {"log": log_ids}

    response = await client.post(
        "/v0/logs/derived",
        json={
            "project_name": project_name,
            "key": key,
            "equation": equation,
            "referenced_logs": referenced_logs,
            "derived": False,  # Create static entries
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # 3. Fetch all logs and verify the integrity of the new static field
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    logs = response.json()["logs"]
    assert len(logs) == 5

    # 4. Check each log to ensure the computed value matches its source value
    for log in logs:
        original_value = log["entries"]["value"]
        computed_value = log["entries"].get(key)

        assert (
            computed_value is not None
        ), f"Log {log['id']} is missing the new static entry"
        assert computed_value == original_value + 100, (
            f"ID misalignment detected for log {log['id']}. "
            f"Expected {original_value + 100}, but got {computed_value}."
        )


@pytest.mark.anyio
async def test_derived_embedding_and_filtering_with_partial_null_values(
    client: AsyncClient,
):
    """
    Test creating embedding derived columns and filtering when some logs have null or empty
    values for the field being embedded. This verifies that embedding operations handle
    partial null values gracefully and filtering still works correctly.
    """
    project = "embed_partial_null_demo"
    await _create_project(client, project)

    # Create base logs with mixed description values
    log_ids = []

    # Log 0: Valid description
    response = await _create_log(
        client,
        project,
        entries={"desc": "a cute little cat", "category": "animal"},
    )
    assert response.status_code == 200, response.json()
    log_ids.append(response.json()["log_event_ids"][0])

    # Log 1: Valid description
    response = await _create_log(
        client,
        project,
        entries={"desc": "a friendly dog", "category": "animal"},
    )
    assert response.status_code == 200, response.json()
    log_ids.append(response.json()["log_event_ids"][0])

    # Log 2: Empty string description
    response = await _create_log(
        client,
        project,
        entries={"desc": "", "category": "empty"},
    )
    assert response.status_code == 200, response.json()
    log_ids.append(response.json()["log_event_ids"][0])

    # Log 3: Null description
    response = await _create_log(
        client,
        project,
        entries={"desc": None, "category": "null"},
    )
    assert response.status_code == 200, response.json()
    log_ids.append(response.json()["log_event_ids"][0])

    # Log 4: Missing description field entirely
    response = await _create_log(client, project, entries={"category": "missing"})
    assert response.status_code == 200
    log_ids.append(response.json()["log_event_ids"][0])

    # Log 5: Valid description
    response = await _create_log(
        client,
        project,
        entries={"desc": "a wooden chair", "category": "furniture"},
    )
    assert response.status_code == 200, response.json()
    log_ids.append(response.json()["log_event_ids"][0])

    # Log 6: Whitespace-only description
    response = await _create_log(
        client,
        project,
        entries={"desc": "   ", "category": "whitespace"},
    )
    assert response.status_code == 200, response.json()
    log_ids.append(response.json()["log_event_ids"][0])

    # Log 7: Valid description
    response = await _create_log(
        client,
        project,
        entries={"desc": "a small kitten", "category": "animal"},
    )
    assert response.status_code == 200
    log_ids.append(response.json()["log_event_ids"][0])

    # Create derived embedding column for descriptions
    # This should succeed even though some logs have null/empty descriptions
    key = "desc_vec"
    equation = "embed({log:desc})"
    referenced_logs = {"log": log_ids}

    response = await _create_derived_entry(
        client,
        project,
        key,
        equation,
        referenced_logs,
    )
    assert (
        response.status_code == 200
    ), f"Expected 200 but got {response.status_code}: {response.text}"

    # Verify that the derived field was created
    response = await client.get(
        f"/v0/logs/fields?project_name={project}",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    fields = response.json()
    assert key in fields
    # Embedding fields should be of type 'list' (vector)
    assert fields[key]["data_type"] in ["list", "array", "vector", "List[float]"]

    # Test filtering by similarity to 'little kitty'
    # This should match logs with valid cat-related descriptions
    filter_expr = "cosine(desc_vec, embed('little kitty')) < 0.5"
    response = await client.get(
        "/v0/logs",
        params={
            "project_name": project,
            "filter_expr": filter_expr,
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Verify filtering results
    logs = response.json()["logs"]
    assert len(logs) == 2, "Expected 2 logs to match the filter"

    # Check that cat-related logs are included
    cat_related_found = False
    kitten_related_found = False
    for log in logs:
        desc = log["entries"].get("desc", "")
        if desc and "cat" in desc:
            cat_related_found = True
        if desc and "kitten" in desc:
            kitten_related_found = True

    assert cat_related_found, "Expected to find cat-related logs in the results"
    assert kitten_related_found, "Expected to find kitten-related logs in the results"

    # Test filtering by similarity to a more specific term
    # This should not match logs with null/empty descriptions
    filter_expr_strict = "cosine(desc_vec, embed('brown furniture')) < 0.7"
    response = await client.get(
        "/v0/logs",
        params={
            "project_name": project,
            "filter_expr": filter_expr_strict,
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    furniture_logs = response.json()["logs"]
    assert len(furniture_logs) == 1, "Expected 1 log to match the filter"

    # Check that chair is found but null/empty description logs are not
    chair_found = False
    null_empty_found = False
    for log in furniture_logs:
        desc = log["entries"].get("desc", "")
        if desc and "chair" in desc:
            chair_found = True
        # Check if any logs with null/empty/missing descriptions are returned
        category = log["entries"].get("category", "")
        if category in ["empty", "null", "missing", "whitespace"]:
            null_empty_found = True

    # Chair should be found for furniture-related query
    assert chair_found, "Expected to find chair in furniture-related query"

    # Logs with null/empty descriptions should not match semantic queries
    assert (
        not null_empty_found
    ), "Logs with null/empty descriptions should not match semantic queries"

    # Test getting all logs to verify that null/empty logs still exist but have null embeddings
    response = await client.get(
        f"/v0/logs?project_name={project}",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    all_logs = response.json()["logs"]

    # Verify we have all 8 logs
    assert len(all_logs) == 8, f"Expected 8 logs but got {len(all_logs)}"

    # Check that logs with null/empty descriptions have null or empty derived embeddings
    null_embedding_count = 0
    for log in all_logs:
        category = log["entries"].get("category", "")
        derived_entries = log.get("derived_entries", {})

        if category in ["empty", "null", "missing", "whitespace"]:
            # These logs should have null or empty embeddings
            embedding = derived_entries.get(key)
            if embedding is None or (
                isinstance(embedding, list) and len(embedding) == 0
            ):
                null_embedding_count += 1

    # We should have some logs with null/empty embeddings
    assert null_embedding_count == 4, "Expected 4 logs to have null/empty embeddings"


@pytest.mark.anyio
@pytest.mark.xdist_group(name="gcs_serial")
async def test_visual_semantic_cache_e2e(client: AsyncClient):
    """
    Tests the Visual Semantic Cache by filtering for a subset of images
    and then sorting them by visual similarity to find the best match.

    Note: Marked with xdist_group to run serially due to GCS eventual consistency issues.
    """
    project_name = "visual_cache_sorting_project"
    context_name = "visual_cache_sorting_context"
    user_id = 1

    await _create_project(client, project_name, user=user_id)

    response = await client.post(
        f"/v0/project/{project_name}/contexts",
        json={"name": context_name},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # 1. Log several images, including two very similar "cat" images
    image_files = {
        "cat_v1": "cat.png",  # this is the original
        "cat_v2": "cat_2.png",  # this is a slightly different version
        "dog": "dog.png",
        "car": "car.png",
    }

    log_ids_map = {}
    for name, path in image_files.items():
        # We'll use a 'type' field to filter on
        image_type = "animal" if "cat" in name or "dog" in name else "vehicle"
        response = await _create_image_log(
            client,
            project_name,
            context_name,
            path,
            {"name": name, "type": image_type},
            image_col_name="img",
        )
        assert response.status_code == 200, response.json()
        log_ids_map[name] = response.json()["log_event_ids"][0]

    # 2. Pre-flight check: Wait for all images to be available in GCS
    await wait_for_gcs_images(client, project_name, context_name, image_col_name="img")

    # 3. Create pHash derived logs (images are now confirmed available)
    phash_key = "image_phash"
    response = await _create_derived_entry(
        client,
        project_name,
        key=phash_key,
        equation=f"phash({{log:img}})",
        referenced_logs={"log": list(log_ids_map.values())},
        context=context_name,
        user=user_id,
    )
    assert response.status_code == 200

    # 4. Verify all animal images have valid pHashes
    response = await client.get(
        f"/v0/logs?project_name={project_name}&context={context_name}"
        f"&filter_expr=type == 'animal'",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    animal_logs = response.json()["logs"]
    assert len(animal_logs) == 3, f"Expected 3 animal logs, got {len(animal_logs)}"

    # Verify each animal image has a valid pHash (16 hex chars)
    for log in animal_logs:
        log_name = log["entries"]["name"]
        phash = log.get("derived_entries", {}).get("image_phash")
        assert phash is not None, (
            f"pHash for '{log_name}' is None despite pre-flight check passing. "
            f"This should not happen - please investigate."
        )
        assert len(phash) == 16 and all(
            c in "0123456789abcdef" for c in phash
        ), f"pHash for '{log_name}' is invalid: {phash!r}. Expected 16 hex characters."

    # 5. Get the pHash of the 'cat_v2' image to use as our query hash
    cat_v2_log = next(log for log in animal_logs if log["entries"]["name"] == "cat_v2")
    cat_v2_phash = cat_v2_log["derived_entries"]["image_phash"]

    # 6. Query for the most visually similar image within the 'animal' type
    sorting_expression = f"phash_distance(image_phash, '{cat_v2_phash}')"

    response = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "context": context_name,
            "filter_expr": "type == 'animal'",  # Filter down to relevant images first
            "sorting": f'{{"{sorting_expression}": "ascending"}}',  # Sort by distance
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, f"Failed to query with sorting: {response.text}"

    # 7. Verify the results
    all_logs = response.json()["logs"]
    assert len(all_logs) == 3, "Expected to find exactly three logs"
    # The top result should be 'cat_v2' itself, as its distance is 0.
    assert (
        all_logs[0]["entries"]["name"] == "cat_v2"
    ), "The best match should be the image itself"
    assert (
        all_logs[1]["entries"]["name"] == "cat_v1"
    ), "The second best match should be the image itself"
    assert (
        all_logs[2]["entries"]["name"] == "dog"
    ), "The third best match should be the dog image"


# =============================================================================
# JSONB-Specific Tests for Derived Log Materialization
# =============================================================================


@pytest.mark.anyio
async def test_derived_field_category_jsonb(client: AsyncClient):
    """
    Test that derived fields have field_category='derived_entry' in FieldType.

    This test verifies that when creating derived logs in either mode,
    the field type is properly marked as 'derived_entry'.
    """
    project_name = "test_field_category"
    await _create_project(client, project_name, user=1)

    # Create base logs
    log_ids = []
    for i in range(3):
        response = await _create_log(client, project_name, entries={"score": i * 10})
        assert response.status_code == 200
        log_ids.append(response.json()["log_event_ids"][0])

    # Create derived log
    key = "double_score"
    equation = "{log:score} * 2"
    referenced_logs = {"log": log_ids}
    response = await _create_derived_entry(
        client,
        project_name,
        key,
        equation,
        referenced_logs,
    )
    assert response.status_code == 200

    # Verify field category
    response = await client.get(
        f"/v0/logs/fields?project_name={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    fields = response.json()

    assert key in fields, f"Derived field '{key}' should exist in fields"
    # The API returns field_category as "field_type" in the response
    assert fields[key].get("field_type") == "derived_entry", (
        f"Derived field should have field_type='derived_entry', "
        f"got '{fields[key].get('field_type')}'"
    )


@pytest.mark.anyio
async def test_no_derived_log_rows_jsonb(client: AsyncClient, monkeypatch):
    """
    Test that no DerivedLog rows are created in JSONB mode.

    In JSONB mode, derived values are stored directly in LogEvent.data,
    not in separate DerivedLog rows.
    """
    # JSONB mode is now always enabled - EAV mode has been removed

    project_name = "test_no_derived_rows"
    await _create_project(client, project_name, user=1)

    # Create base logs
    log_ids = []
    for i in range(3):
        response = await _create_log(client, project_name, entries={"value": i * 5})
        assert response.status_code == 200
        log_ids.append(response.json()["log_event_ids"][0])

    # Create derived log
    key = "computed_value"
    equation = "{log:value} + 100"
    referenced_logs = {"log": log_ids}
    response = await _create_derived_entry(
        client,
        project_name,
        key,
        equation,
        referenced_logs,
    )
    assert response.status_code == 200

    # Verify derived values exist in logs (they should be in entries, not derived_entries in JSONB mode)
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    logs = response.json()["logs"]

    # In JSONB mode, values are materialized into LogEvent.data
    # They should still be accessible (either in entries or derived_entries depending on API response format)
    for log in logs:
        # Check that the derived value exists somewhere
        has_derived_value = key in log.get("entries", {}) or key in log.get(
            "derived_entries",
            {},
        )
        assert (
            has_derived_value
        ), f"Derived field '{key}' should exist for log {log['id']}"


@pytest.mark.anyio
async def test_referenced_keys_populated_on_template_creation(
    client: AsyncClient,
    monkeypatch,
    dbsession,
):
    """Verify that referenced_keys is populated when creating derived log templates."""
    from orchestra.db.models.orchestra_models import ActiveDerivedLog

    project_name = "test_referenced_keys"
    await _create_project(client, project_name, user=1)

    # Create base logs
    response = await _create_log(
        client,
        project_name,
        entries={"score": 0.8, "accuracy": 0.9},
    )
    assert response.status_code == 200
    log_id = response.json()["log_event_ids"][0]

    # Create derived log with equation referencing both fields
    equation = "{log0:score} + {log0:accuracy}"
    response = await _create_derived_entry(
        client,
        project_name,
        key="total",
        equation=equation,
        referenced_logs={"log0": {"filter_expr": "True"}},
    )
    assert response.status_code == 200

    # Verify referenced_keys was populated
    dbsession.expire_all()  # Clear cache to get fresh data
    template = (
        dbsession.query(ActiveDerivedLog)
        .filter(ActiveDerivedLog.key == "total")
        .order_by(ActiveDerivedLog.id.desc())
        .first()
    )

    assert template is not None, "ActiveDerivedLog template should exist"
    assert template.referenced_keys is not None, "referenced_keys should be populated"
    assert set(template.referenced_keys) == {"score", "accuracy"}, (
        f"referenced_keys should contain ['score', 'accuracy'], "
        f"got {template.referenced_keys}"
    )


@pytest.mark.anyio
async def test_referenced_keys_updated_on_template_update(
    client: AsyncClient,
    monkeypatch,
    dbsession,
):
    """Verify that referenced_keys is updated when modifying derived log templates."""
    from datetime import datetime, timezone

    from orchestra.db.dao.derived_log_dao import _extract_field_names_from_equation
    from orchestra.db.models.orchestra_models import ActiveDerivedLog, Context, Project

    # JSONB mode is now always enabled - EAV mode has been removed

    project_name = "test_referenced_keys_update"
    await _create_project(client, project_name, user=1)

    # Create base log
    response = await _create_log(
        client,
        project_name,
        entries={"field_a": 10, "field_b": 20, "field_c": 30},
    )
    assert response.status_code == 200
    log_id = response.json()["log_event_ids"][0]

    # Get project_id and context_id for direct template creation
    project_obj = dbsession.query(Project).filter(Project.name == project_name).first()
    assert project_obj is not None
    context_obj = (
        dbsession.query(Context)
        .filter(
            Context.project_id == project_obj.id,
            Context.name == "",  # Default context
        )
        .first()
    )
    assert context_obj is not None

    # Directly create an ActiveDerivedLog template with initial referenced_keys
    initial_equation = "{log:field_a} * 2"
    initial_referenced_keys = _extract_field_names_from_equation(initial_equation)
    template = ActiveDerivedLog(
        project_id=project_obj.id,
        context_id=context_obj.id,
        key="computed",
        equation=initial_equation,
        referenced_logs={"log": {"filter_expr": "True"}},
        filter_expression={"log": {"filter_expr": "True"}},
        inferred_type="float",
        referenced_keys=initial_referenced_keys,
        is_active=True,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    dbsession.add(template)
    dbsession.commit()

    # Verify initial referenced_keys
    dbsession.expire_all()
    template = (
        dbsession.query(ActiveDerivedLog)
        .filter(ActiveDerivedLog.key == "computed")
        .first()
    )
    assert template is not None
    assert set(template.referenced_keys) == {"field_a"}

    # Update the derived log template equation via the API
    updated_equation = "{log:field_b} + {log:field_c}"
    response = await client.put(
        "/v0/logs/derived",
        json={
            "project_name": project_name,
            "target_derived_logs": {"from_fields": "computed"},
            "key": "computed",
            "equation": updated_equation,
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, f"Update failed: {response.json()}"

    # Verify referenced_keys was updated
    dbsession.expire_all()
    updated_template = (
        dbsession.query(ActiveDerivedLog)
        .filter(ActiveDerivedLog.key == "computed")
        .first()
    )
    assert updated_template is not None
    assert set(updated_template.referenced_keys) == {"field_b", "field_c"}, (
        f"referenced_keys should be updated to ['field_b', 'field_c'], "
        f"got {updated_template.referenced_keys}"
    )


@pytest.mark.anyio
async def test_ripple_effect_jsonb(client: AsyncClient, monkeypatch):
    """
    Test the ripple effect in JSONB mode: when base fields are updated,
    dependent derived fields are automatically recomputed.

    Uses GET /v0/logs API (now JSONB-aware) to verify results.
    """
    # JSONB mode is now always enabled - EAV mode has been removed

    project_name = "test_ripple_effect"
    await _create_project(client, project_name, user=1)

    # Create base logs
    log_ids = []
    for i in range(2):
        response = await _create_log(
            client,
            project_name,
            entries={"base_value": i + 1},
        )
        assert response.status_code == 200
        log_ids.append(response.json()["log_event_ids"][0])

    # Create derived log with filter_expr (to create ActiveDerivedLog template)
    key = "derived_from_base"
    equation = "{log:base_value} * 10"
    referenced_logs = {"log": {"filter_expr": "True"}}
    response = await _create_derived_entry(
        client,
        project_name,
        key,
        equation,
        referenced_logs,
    )
    assert (
        response.status_code == 200
    ), f"Failed to create derived entry: {response.json()}"

    # Verify initial derived values via GET /v0/logs API
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    logs = response.json()["logs"]

    for log in logs:
        log_id = log.get("log_event_id") or log.get("id")
        entries = log.get("entries", {})
        derived_entries = log.get("derived_entries", {})
        base_val = entries.get("base_value")
        derived_value = derived_entries.get(key)

        if derived_value is not None and base_val is not None:
            assert (
                derived_value == base_val * 10
            ), f"Initial derived value should be {base_val * 10}, got {derived_value}"

    # Update base value for first log
    update_payload = {
        "logs": [log_ids[0]],
        "entries": [{"base_value": 100}],
        "overwrite": True,
    }
    response = await client.put(
        "/v0/logs",
        json=update_payload,
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Verify ripple effect via GET /v0/logs API
    response = await client.get(
        f"/v0/logs?project_name={project_name}&from_ids={log_ids[0]}",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    logs = response.json()["logs"]
    assert len(logs) == 1, f"Expected 1 log, got {len(logs)}"

    updated_log = logs[0]
    entries = updated_log.get("entries", {})
    derived_entries = updated_log.get("derived_entries", {})

    # Verify base value was updated
    assert entries.get("base_value") == 100, "Base value should be updated to 100"

    # Verify derived value was recomputed (ripple effect)
    # Expected: 100 * 10 = 1000
    derived_value = derived_entries.get(key)
    assert (
        derived_value == 1000
    ), f"Derived value should be recomputed to 1000, got {derived_value}"


@pytest.mark.anyio
async def test_active_derived_log_materialization_jsonb(
    client: AsyncClient,
    monkeypatch,
):
    """
    Test that the admin endpoint correctly materializes active derived log templates
    in JSONB mode.

    Uses GET /v0/logs API (now JSONB-aware) to verify results.
    """
    # JSONB mode is now always enabled - EAV mode has been removed

    project_name = "test_admin_materialization"
    await _create_project(client, project_name, user=1)

    # Create initial base logs
    initial_log_ids = []
    for score in [10, 20, 30]:
        response = await _create_log(
            client,
            project_name,
            entries={"score": score},
        )
        assert response.status_code == 200
        initial_log_ids.append(response.json()["log_event_ids"][0])

    # Create derived log with filter_expr (creates ActiveDerivedLog template)
    key = "score_doubled"
    equation = "{log:score} * 2"
    referenced_logs = {"log": {"filter_expr": ""}}
    response = await _create_derived_entry(
        client,
        project_name,
        key,
        equation,
        referenced_logs,
    )
    assert (
        response.status_code == 200
    ), f"Failed to create derived entry: {response.json()}"

    # Verify initial derived values via GET /v0/logs API
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    logs = response.json()["logs"]

    for log in logs:
        entries = log.get("entries", {})
        derived_entries = log.get("derived_entries", {})
        score = entries.get("score")
        derived_value = derived_entries.get(key)

        if derived_value is not None and score is not None:
            assert (
                derived_value == score * 2
            ), f"Derived value should be {score * 2}, got {derived_value}"

    # Create a new log (without derived entry yet)
    new_score = 50
    response = await _create_log(
        client,
        project_name,
        entries={"score": new_score},
    )
    assert response.status_code == 200
    new_log_id = response.json()["log_event_ids"][0]

    # Verify new log doesn't have derived entry yet via API
    response = await client.get(
        f"/v0/logs?project_name={project_name}&from_ids={new_log_id}",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    logs = response.json()["logs"]
    assert len(logs) == 1
    new_log = logs[0]
    assert key not in new_log.get(
        "derived_entries",
        {},
    ), "New log should not have derived entry yet"

    # Call admin endpoint to process active derived logs
    admin_headers = HEADERS.copy()
    admin_headers["Authorization"] = f"Bearer {os.environ['ORCHESTRA_ADMIN_KEY']}"

    response = await client.post(
        "/v0/admin/update_active_derived_logs",
        headers=admin_headers,
    )
    assert response.status_code == 200

    # Verify new log now has derived entry via API
    response = await client.get(
        f"/v0/logs?project_name={project_name}&from_ids={new_log_id}",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    logs = response.json()["logs"]
    assert len(logs) == 1
    updated_log = logs[0]

    # In JSONB mode, the derived value should be materialized into LogEvent.data
    derived_value = updated_log.get("derived_entries", {}).get(key)
    assert (
        derived_value == new_score * 2
    ), f"Derived value should be {new_score * 2}, got {derived_value}"


@pytest.mark.anyio
async def test_derived_embedding_filtering_and_sorting_jsonb(
    client: AsyncClient,
    monkeypatch,
):
    """
    Test creating derived text embeddings, filtering by cosine similarity,
    and sorting by similarity distance in JSONB mode.

    This test verifies the full embedding workflow:
    1. Create logs with text descriptions
    2. Create derived embedding column using embed()
    3. Filter logs by cosine similarity to a query
    4. Sort logs by similarity (ascending = most similar first)
    """
    # JSONB mode is now always enabled - EAV mode has been removed

    project_name = "test_embed_filter_sort"
    await _create_project(client, project_name, user=1)

    # 1) Create base logs with various text descriptions
    descriptions = [
        ("a cute little cat playing", "animal"),
        ("a friendly golden retriever dog", "animal"),
        ("a small fluffy kitten sleeping", "animal"),
        ("a red sports car racing", "vehicle"),
        ("a blue wooden chair", "furniture"),
        ("a brown leather sofa", "furniture"),
    ]

    log_ids = []
    for desc, category in descriptions:
        response = await _create_log(
            client,
            project_name,
            entries={"description": desc, "category": category},
        )
        assert response.status_code == 200, response.json()
        log_ids.append(response.json()["log_event_ids"][0])

    # 2) Create derived embedding column
    key = "desc_embedding"
    equation = "embed({log:description})"
    referenced_logs = {"log": log_ids}

    response = await _create_derived_entry(
        client,
        project_name,
        key,
        equation,
        referenced_logs,
    )
    assert response.status_code == 200, f"Failed to create embedding: {response.text}"

    # Verify embeddings were created
    response = await client.get(
        f"/v0/logs/fields?project_name={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    fields = response.json()
    assert key in fields, f"Embedding field '{key}' should exist"
    assert fields[key]["data_type"] in ["list", "array", "vector", "List[float]"]

    # 3) Test filtering by cosine similarity
    # Query for "cute kitty" - should match cat and kitten descriptions more closely
    # Using a threshold of 0.5 which is reasonable for semantic similarity with OpenAI embeddings
    # (cosine distance of 0.25-0.5 indicates high similarity for non-identical text)
    filter_expr = "cosine(desc_embedding, embed('cute kitty')) < 0.5"
    response = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "filter_expr": filter_expr,
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    filtered_logs = response.json()["logs"]

    # Should find at least the cat and kitten related logs
    assert (
        len(filtered_logs) >= 1
    ), f"Expected at least 1 matching log, got {len(filtered_logs)}"

    # Verify cat or kitten descriptions are in results
    filtered_descriptions = [log["entries"]["description"] for log in filtered_logs]
    cat_or_kitten_found = any(
        "cat" in desc.lower() or "kitten" in desc.lower()
        for desc in filtered_descriptions
    )
    assert (
        cat_or_kitten_found
    ), f"Expected to find 'cat' or 'kitten' in results: {filtered_descriptions}"

    # 4) Test dynamic expression sorting by similarity
    # Sort all logs by similarity to "fluffy pet" (ascending = most similar first)
    sort_expr = "cosine(desc_embedding, embed('fluffy pet'))"
    sorting = json.dumps({sort_expr: "ascending"})

    response = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "sorting": sorting,
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, f"Sorting failed: {response.json()}"
    sorted_logs = response.json()["logs"]

    assert len(sorted_logs) == 6, f"Expected 6 logs, got {len(sorted_logs)}"

    # The first results should be animal-related (cats, dogs, kittens)
    # and furniture/vehicles should be later
    first_categories = [log["entries"]["category"] for log in sorted_logs[:3]]
    assert all(
        cat == "animal" for cat in first_categories
    ), f"Expected top 3 to be animals, got categories: {first_categories}"

    last_categories = [log["entries"]["category"] for log in sorted_logs[-2:]]
    assert all(
        cat in ["furniture", "vehicle"] for cat in last_categories
    ), f"Expected last 2 to be furniture/vehicle, got: {last_categories}"

    # 5) Test combined filtering and sorting
    # Filter to only animals, then sort by similarity to "puppy dog"
    filter_expr_animals = "category == 'animal'"
    sort_expr_dog = "cosine(desc_embedding, embed('puppy dog'))"
    sorting_dog = json.dumps({sort_expr_dog: "ascending"})

    response = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "filter_expr": filter_expr_animals,
            "sorting": sorting_dog,
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, f"Filter+sort failed: {response.json()}"
    filtered_sorted_logs = response.json()["logs"]

    assert (
        len(filtered_sorted_logs) == 3
    ), f"Expected 3 animal logs, got {len(filtered_sorted_logs)}"

    # The dog description should be first (most similar to "puppy dog")
    first_desc = filtered_sorted_logs[0]["entries"]["description"]
    assert (
        "dog" in first_desc.lower() or "retriever" in first_desc.lower()
    ), f"Expected dog to be most similar to 'puppy dog', got: {first_desc}"

    # 6) Test descending sort (least similar first)
    sorting_desc = json.dumps({sort_expr: "descending"})
    response = await client.get(
        "/v0/logs",
        params={
            "project_name": project_name,
            "sorting": sorting_desc,
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    desc_sorted_logs = response.json()["logs"]

    # With descending sort, furniture/vehicle should come first (least similar to "fluffy pet")
    first_desc_categories = [log["entries"]["category"] for log in desc_sorted_logs[:2]]
    assert all(
        cat in ["furniture", "vehicle"] for cat in first_desc_categories
    ), f"Expected first 2 to be furniture/vehicle with desc sort, got: {first_desc_categories}"


@pytest.mark.anyio
async def test_create_derived_entry_both_modes(client: AsyncClient):
    """
    Test creating derived entries works correctly in both EAV and JSONB modes.

    This parametrized test ensures feature parity between modes.
    """
    project_name = "test_derived_both_modes"
    await _create_project(client, project_name, user=1)

    # Create base logs
    log_ids = []
    values = [10, 20, 30]
    for val in values:
        response = await _create_log(client, project_name, entries={"input": val})
        assert response.status_code == 200
        log_ids.append(response.json()["log_event_ids"][0])

    # Create derived log
    key = "output"
    equation = "{log:input} + 5"
    referenced_logs = {"log": log_ids}
    response = await _create_derived_entry(
        client,
        project_name,
        key,
        equation,
        referenced_logs,
    )
    assert response.status_code == 200
    assert "Created" in response.json()["info"]

    # Verify derived values
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    logs = response.json()["logs"]

    for log in logs:
        input_val = log["entries"]["input"]
        derived_val = log.get("derived_entries", {}).get(key)
        if derived_val is not None:
            assert (
                derived_val == input_val + 5
            ), f"Derived value should be {input_val + 5}, got {derived_val}"


@pytest.mark.anyio
async def test_update_derived_entry_both_modes(client: AsyncClient):
    """
    Test updating derived entries works correctly in both EAV and JSONB modes.

    This parametrized test ensures feature parity between modes.
    """
    project_name = "test_update_derived_both"
    await _create_project(client, project_name, user=1)

    # Create base logs
    log_ids = []
    for i in range(2):
        response = await _create_log(client, project_name, entries={"num": i + 1})
        assert response.status_code == 200
        log_ids.append(response.json()["log_event_ids"][0])

    # Create initial derived log
    key = "computed"
    equation = "{log:num} * 2"
    referenced_logs = {"log": log_ids}
    response = await _create_derived_entry(
        client,
        project_name,
        key,
        equation,
        referenced_logs,
    )
    assert response.status_code == 200

    # Update the derived log equation
    response = await client.put(
        "/v0/logs/derived",
        json={
            "project_name": project_name,
            "target_derived_logs": {"from_fields": key},
            "key": key,
            "equation": "{log:num} * 3",  # Changed multiplier
            "referenced_logs": {"log": log_ids},
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Verify updated values
    response = await client.get(
        f"/v0/logs?project_name={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    logs = response.json()["logs"]

    for log in logs:
        num_val = log["entries"]["num"]
        derived_val = log.get("derived_entries", {}).get(key)
        if derived_val is not None:
            # After update, should be num * 3
            assert (
                derived_val == num_val * 3
            ), f"Updated derived value should be {num_val * 3}, got {derived_val}"
