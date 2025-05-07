import json

import pytest
from httpx import AsyncClient

from . import (
    HEADERS,
    _create_derived_entry,
    _create_log,
    _create_project,
    _create_several_logs,
    _delete_logs,
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
async def test_create_derived_entry_with_filter_expr(client: AsyncClient):
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
async def test_update_derived_entry_with_referenced_logs(client: AsyncClient):
    project_name = "test_update_derived_refs"
    await _create_project(client, project_name)

    # 1. Create base logs with temperature values
    base_log_ids = []
    temps = [20.0, 25.0, 30.0, 35.0]  # Four base logs
    for temp in temps:
        resp = await client.post(
            "/v0/logs",
            json={"project": project_name, "entries": {"temperature": temp}},
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
    resp = await client.get(f"/v0/logs?project={project_name}", headers=HEADERS)
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
            "project": project_name,
            "target_derived_logs": {"from_fields": "temp_plus_10"},
            "key": "temp_plus_10",
            "equation": "{t:temperature} + 20",  # Modified equation
            "referenced_logs": new_referenced_logs,
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200

    # 4. Verify the updates
    resp = await client.get(f"/v0/logs?project={project_name}", headers=HEADERS)
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
            json={"project": project_name, "entries": {"temperature": temp}},
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
            "project": project_name,
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
    resp = await client.get(f"/v0/logs?project={project_name}", headers=HEADERS)
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
        params={"project": project_name},
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
            "project": project_name,
            "column_context": "_/",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200
    data_context = resp.json()
    logs_context = data_context["logs"]
    for log_obj in logs_context:
        # the "entries" keys should not have "a/b/param1" or any param
        for k in log_obj["entries"]:
            assert not k.startswith("a/b/"), f"Found param key in context=_/: {k}"

    # 6) Test a filter_expr,
    filter_expr = "_/temperature > 100"
    resp = await client.get(
        "/v0/logs",
        params={
            "project": project_name,
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
            "project": project_name,
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
            "project": project_name,
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
async def test_update_logs_and_derived_logs_are_updated(client: AsyncClient):
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
        params={"project": project_name},
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
    response = await client.get(f"/v0/logs?project={project_name}", headers=HEADERS)
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
    response = await client.get(f"/v0/logs?project={project_name}", headers=HEADERS)
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
            json={"project": project_name, "entries": log_data["entries"]},
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
        f"/v0/logs?project={project_name}",
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
            "project": project_name,
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
        f"/v0/logs?project={project_name}",
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
        f"/v0/logs?project={project_name}",
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
        f"/v0/logs?project={project_name}",
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
        f"/v0/logs?project={project_name}",
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
async def test_advanced_comprehensions_and_conditionals(client: AsyncClient, test_case):
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
        f"/v0/logs?project={project}&filter_expr={field} is not None",
        headers=HEADERS,
    )
    assert response.status_code == 200
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
            "project": project_name,
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
        f"/v0/logs?project={project_name}",
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
async def test_create_derived_embed_on_column(client: AsyncClient, monkeypatch):
    """
    Test that embed({log:text}) in a derived log equation embeds each log's text field.
    """
    # Stub embed to return a vector based on text for visibility
    def fake_embed(text, model="text-embedding-3-large"):
        # simple stub: map each char to its ord mod 1.0
        return [float(ord(c) % 10) for c in text]

    monkeypatch.setattr(
        "orchestra.vector.utils.embed",
        fake_embed,
    )

    project_name = "test_embed_column_derived"
    await _create_project(client, project_name)

    # Create base logs with a 'text' entry
    texts = ["abc", "xyz"]
    log_ids = []
    for txt in texts:
        resp = await _create_log(client, project_name, entries={"text": txt})
        assert resp.status_code == 200
        log_ids.append(resp.json()["log_event_ids"][0])

    # Derive embedding from each log's 'text' field
    key = "emb_col"
    equation = "embed({log:text})"
    response = await _create_derived_entry(
        client,
        project_name,
        key=key,
        equation=equation,
        referenced_logs={"log": log_ids},
    )
    assert response.status_code == 200

    # Fetch logs and verify derived_entries embedding matches fake_embed
    get_resp = await client.get(f"/v0/logs?project={project_name}", headers=HEADERS)
    assert get_resp.status_code == 200
    logs = get_resp.json()["logs"]
    for log in logs:
        text_val = log["entries"]["text"]
        expected = fake_embed(text_val)
        assert log["derived_entries"][key] == expected


# @pytest.mark.anyio
# @pytest.mark.parametrize(
#     "func_name, expected",
#     [
#         ("l2", math.sqrt((1.0 - 0.0) ** 2 + (0.0 - 1.0) ** 2)),
#         ("l1", abs(1.0 - 0.0) + abs(0.0 - 1.0)),
#         ("ip", 1.0 * 0.0 + 0.0 * 1.0),
#         ("cosine", 1 - ((1.0 * 0.0 + 0.0 * 1.0) / (math.sqrt(1.0) * math.sqrt(1.0)))),
#         ("hamming", 2),
#         ("jaccard", 0),
#     ],
# )
# async def test_create_derived_vector_distance_functions(
#     client: AsyncClient,
#     func_name,
#     expected,
# ):
#     """
#     Test that vector distance functions compute expected distances directly from provided vector fields 'c' and 'd'.
#     """
#     project_name = "test_vector_distance_derived"
#     await _create_project(client, project_name)

#     # Create dummy logs
#     resp = await _create_log(
#         client,
#         project_name,
#         entries={"c": [1.0, 0.0], "d": [0.0, 1.0]},
#     )
#     assert resp.status_code == 200

#     # Compute and verify for the parameterized function
#     key = f"dist_{func_name}"
#     equation = f"{func_name}({{log:c}}, {{log:d}})"
#     response = await _create_derived_entry(
#         client,
#         project_name,
#         key=key,
#         equation=equation,
#         referenced_logs={"log": [resp.json()["log_event_ids"][0]]},
#     )
#     assert response.status_code == 200, f"{func_name} creation failed: {response.text}"

#     # Fetch logs and verify derived distance
#     get_resp = await client.get(f"/v0/logs?project={project_name}", headers=HEADERS)
#     assert get_resp.status_code == 200
#     logs = get_resp.json()["logs"]
#     value = logs[0]["derived_entries"][key]
#     assert (
#         pytest.approx(value, rel=1e-6) == expected
#     ), f"{func_name} expected {expected}, got {value}"
