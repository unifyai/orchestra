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
        log_ids.append(response.json()[0])

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
    data = response.json()
    assert "derived_log_ids" in data
    assert (
        len(data["derived_log_ids"]) == 3
    )  # Should create one derived log per base log


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
    data = response.json()
    # Only logs with score > 20 (i=3,4) should have derived logs
    assert "derived_log_ids" in data
    assert len(data["derived_log_ids"]) == 2


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
        base_log_ids.append(resp.json()[0])

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
    initial_derived_ids = resp.json()["derived_log_ids"]
    assert (
        len(initial_derived_ids) == 2
    ), "Should create derived logs for first two base logs"

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
        base_log_ids.append(resp.json()[0])

    # 2) Create first derived log: "temp_plus_10"
    resp = await _create_derived_entry(
        client,
        project_name,
        key="temp_plus_10",
        equation="{t:temperature} + 10",
        referenced_logs={"t": [base_log_ids[0], base_log_ids[1]]},
    )
    assert resp.status_code == 200
    derived_ids_1 = resp.json()["derived_log_ids"]
    assert len(derived_ids_1) == 2

    # 3) Create second derived log: "temp_minus_5"
    resp = await _create_derived_entry(
        client,
        project_name,
        key="temp_minus_5",
        equation="{t:temperature} - 5",
        referenced_logs={"t": [base_log_ids[2]]},
    )
    assert resp.status_code == 200
    derived_ids_2 = resp.json()["derived_log_ids"]
    assert len(derived_ids_2) == 1

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
            "key": "temp_times_3",
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
    num_times_3 = 0
    num_minus_5 = 0

    for log_obj in data["logs"]:
        derived_entries = log_obj.get("derived_entries", {})
        # Check if they have "temp_times_3"
        if "temp_times_3" in derived_entries:
            num_times_3 += 1
            # verify correctness of the computed value
            base_temp = log_obj["entries"].get("temperature")
            assert derived_entries["temp_times_3"] == base_temp * 3
        if "temp_minus_5" in derived_entries:
            num_minus_5 += 1
            # verify correctness
            base_temp = log_obj["entries"].get("temperature")
            assert derived_entries["temp_minus_5"] == base_temp - 5

    # We expect 2 logs with "temp_times_3" (the old plus_10 ones)
    assert num_times_3 == 2
    # We expect 1 log with "temp_minus_5"
    assert num_minus_5 == 1

    # Also ensure the old "temp_plus_10" is gone
    for log_obj in data["logs"]:
        derived_entries = log_obj.get("derived_entries", {})
        assert "temp_plus_10" not in derived_entries


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
    derived_log_ids1 = resp.json()["derived_log_ids"]
    assert len(derived_log_ids1) == len(base_log_ids1)

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
    derived_log_ids2 = resp.json()["derived_log_ids"]
    assert len(derived_log_ids2) == len(base_log_ids2)

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
    derived_log_ids3 = resp.json()["derived_log_ids"]
    assert len(derived_log_ids3) == len(base_log_ids3)

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
        base_log_ids.append(response.json()[0])

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
        "ids": base_log_ids,
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
        log_ids.append(response.json()[0])

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
    derived_log_ids1 = response.json()["derived_log_ids"]
    assert len(derived_log_ids1) == 3

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
    derived_log_ids2 = response.json()["derived_log_ids"]
    assert len(derived_log_ids2) == 3

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
