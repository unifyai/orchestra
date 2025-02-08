import base64
import json
import os
from datetime import datetime, timezone
from typing import List, Optional

import cv2
import numpy as np
import pytest
from httpx import AsyncClient, Request

from ..web.api.log.helpers import _is_all_unique, reduction_methods

api_key = str(os.getenv("AUTH_ACCOUNT_API_KEY"))
api_key_second_user = "2nd_api_key"

HEADERS = {
    "accept": "application/json",
    "Authorization": f"Bearer {api_key}",
}

HEADERS_2 = {
    "accept": "application/json",
    "Authorization": f"Bearer {api_key_second_user}",
}

log_data = {
    "logs_for_group_threshold": [
        {
            "shared_string": "common value",
            "unique_string": "value1",
            "shared_number": 42,
            "unique_number": 1,
            "shared_object": {"key": "value"},
            "mixed_field": "appears twice",
        },
        {
            "shared_string": "common value",
            "unique_string": "value2",
            "shared_number": 42,
            "unique_number": 2,
            "shared_object": {"key": "value"},
            "mixed_field": "appears twice",
        },
        {
            "shared_string": "common value",
            "unique_string": "value3",
            "shared_number": 42,
            "unique_number": 3,
            "shared_object": {"key": "value"},
            "mixed_field": "unique value",
        },
        {
            "shared_string": "common value",
            "unique_string": "value4",
            "shared_number": 42,
            "unique_number": 4,
            "shared_object": {"key": "value"},
            "mixed_field": "another unique",
        },
    ],
    "log": {
        "a/b/c/input": "Some input data",
        "a/b/c/boolean_input": True,
        "a/b/c/numeric_input": 4.5,
    },
    "log_update": {
        "my_list": ["a", "b", "c"],
        "my_dict": {"a": 1, "b": 2, "c": 3},
    },
    "log_update_w_overwrite": {
        "a/b/c/boolean_input": False,
        "a/b/c/numeric_input": 5.4,
    },
    "logs_for_grouping": [
        {
            "a/input": "What is 1 + 1?",
            "system_prompt": "You are an expert mathematician.",
        },
        {
            "a/input": "What is 2 + 2?",
            "system_prompt": "You are an expert mathematician.",
        },
        {
            "a/input": "What is 1 + 1?",
            "system_prompt": "Respond only with a single digit.",
        },
        {
            "input": "What is 2 + 2?",
            "system_prompt": "Respond only with a single digit.",
        },
    ],
    "logs_for_various": [
        {
            "_/description": "boiling water",
            "_/temperature": 100.0,
            "_/state": "liquid->gas",
            "_/safe": False,
            "_/timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
        },
        {
            "_/description": "freezing water",
            "_/temperature": 0.0,
            "_/state": "liquid->solid",
            "_/safe": True,
            "_/timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
        },
        {
            "_/description": "surface of the sun",
            "_/temperature": 6000.0,
            "_/state": "gas",
            "_/safe": False,
            "_/timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
        },
        {
            "_/description": "freezing nitrogen",
            "_/temperature": -210.0,
            "_/state": "liquid->solid",
            "_/safe": False,
            "_/timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
        },
        {
            "_/description": "lava",
            "_/metadata": [1, 5, 6],
            "_/_data": {"a": 2, "b": 4},
            "_/timestamp": (datetime(1993, 3, 24, tzinfo=timezone.utc)).isoformat(),
        },
        {
            "_/description": "air",
            "_/metadata": [3, 8, 5],
            "_/_data": {"a": 6, "b": 12, "c": 8, "d": 11},
            "_/timestamp": (datetime(1993, 3, 24, tzinfo=timezone.utc)).isoformat(),
        },
        {
            "_/_data": {"a": 8, "b": 10},
            "_/timestamp": (datetime(1993, 3, 24, tzinfo=timezone.utc)).isoformat(),
        },
    ],
}


def _create_log(client, project_name, user=1, params=None, entries=None):
    _headers = HEADERS if user == 1 else HEADERS_2
    if entries is None:
        entries = log_data["log"]
    if params is None:
        params = {"a/b/param1": "test"}
    # set all entries and params to be mutable (backwards compatibility)
    explicit_types_entries = {k: {"mutable": True} for k in entries.keys()}
    explicit_types_params = {k: {"mutable": True} for k in params.keys()}
    entries["explicit_types"] = explicit_types_entries
    params["explicit_types"] = explicit_types_params
    return client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "params": params,
            "entries": entries,
        },
        headers=_headers,
    )


def _create_derived_entry(client, project_name, key, equation, referenced_logs, user=1):
    _headers = HEADERS if user == 1 else HEADERS_2
    return client.post(
        "/v0/logs/derived",
        json={
            "project": project_name,
            "key": key,
            "equation": equation,
            "referenced_logs": referenced_logs,
        },
        headers=_headers,
    )


async def fetch_logs(client, project_name, **query_params):
    default_params = {
        "project": project_name,
        "sorting": json.dumps({"id": "ascending"}),
    }
    default_params.update(query_params)
    resp = await client.get("/v0/logs", params=default_params, headers=HEADERS)
    assert resp.status_code == 200, resp.text
    return resp.json()["logs"]


def _get_log(client, project_name, log_id, user=1):
    _headers = HEADERS if user == 1 else HEADERS_2
    return client.get(
        f"/v0/logs?project={project_name}",
        params={"from_ids": f"{log_id}"},
        headers=_headers,
    )


def _update_multiple_logs_w_overwrite(client, log_ids, overwrite, user=1):
    _headers = HEADERS if user == 1 else HEADERS_2
    return client.put(
        f"/v0/logs",
        json={
            "ids": log_ids,
            "entries": log_data["log_update_w_overwrite"],
            "overwrite": overwrite,
        },
        headers=_headers,
    )


# Helper function to delete multiple logs
def _delete_logs(client, log_ids, user=1):
    _headers = HEADERS if user == 1 else HEADERS_2
    request = Request(
        "DELETE",
        str(client.base_url) + "/v0/logs",
        json={"ids_and_fields": log_ids},
        headers=_headers,
    )
    return client.send(request)


def _delete_derived_logs(client, project_name, derived_log_ids, user=1):
    _headers = HEADERS if user == 1 else HEADERS_2
    request = Request(
        "DELETE",
        str(client.base_url) + "/v0/logs/derived",
        json={"project": project_name, "target_derived_logs": derived_log_ids},
        headers=_headers,
    )
    return client.send(request)


def _update_logs(client, log_ids, entries, user=1):
    _headers = HEADERS if user == 1 else HEADERS_2
    return client.put(
        "/v0/logs",
        json={"ids": log_ids, "entries": entries},
        headers=_headers,
    )


def _delete_log_fields_from_logs(client, fields, delete_empty_logs=False, user=1):
    _headers = HEADERS if user == 1 else HEADERS_2
    request = Request(
        "DELETE",
        str(client.base_url) + f"/v0/logs",
        params={"delete_empty_logs": delete_empty_logs},
        json={"ids_and_fields": fields},
        headers=_headers,
    )
    return client.send(request)


async def _create_logs_for_grouping(client, project_name, user=1):
    _headers = HEADERS if user == 1 else HEADERS_2
    data = log_data["logs_for_grouping"]
    for i in range(len(data)):
        # Split into params and entries
        entries = {}
        if "a/input" in data[i]:
            entries["a/input"] = data[i]["a/input"]
        elif "input" in data[i]:
            entries["a/input"] = data[i]["input"]

        response = await _create_log(
            client,
            project_name,
            params={"system_prompt": data[i]["system_prompt"]},
            entries=entries,
        )
        assert response.status_code == 200, response.json()


async def _create_logs_for_group_threshold(client, project_name, user=1):
    data = log_data["logs_for_group_threshold"]
    for i in range(len(data)):
        response = await _create_log(client, project_name, entries=data[i])
        assert response.status_code == 200, response.json()


async def _create_several_logs(client, project_name, user=1):
    data = log_data["logs_for_various"]
    for i in range(len(data)):
        response = await _create_log(
            client,
            project_name,
            params={"a/b/param1": f"test_{i}"},
            entries=data[i],
        )
        assert response.status_code == 200, response.json()


def _create_project(client, project_name, user=1):
    _headers = HEADERS if user == 1 else HEADERS_2
    url = "/v0/project"
    project_data = {"name": project_name}
    return client.post(url, json=project_data, headers=_headers)


@pytest.mark.anyio
async def test_create_logs(client: AsyncClient):
    project_name = "eval-project"
    _ = await _create_project(client, project_name)

    # Test single log creation
    response = await _create_log(client, project_name)
    assert response.status_code == 200, response.json()
    log_event_ids = response.json()
    assert isinstance(log_event_ids, list) and len(log_event_ids) == 1
    assert isinstance(log_event_ids[0], int)

    # Test batch log creation with multiple entries
    batch_entries = [
        {"a/b/c/input": "Batch input 1", "a/b/c/numeric_input": 1.5},
        {"a/b/c/input": "Batch input 2", "a/b/c/numeric_input": 2.5},
        {"a/b/c/input": "Batch input 3", "a/b/c/numeric_input": 3.5},
    ]
    batch_params = [
        {"a/b/param1": "test"},
        {"a/b/param2": "test"},
        {"a/b/param3": "test"},
    ]
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "params": batch_params,
            "entries": batch_entries,
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    log_event_ids = response.json()
    assert isinstance(log_event_ids, list)
    assert len(log_event_ids) == 3
    assert all(isinstance(id, int) for id in log_event_ids)
    assert sorted(log_event_ids) == list(
        range(min(log_event_ids), max(log_event_ids) + 1),
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

    # Delete only the first derived log
    response = await _delete_derived_logs(client, project_name, derived_log_ids1)
    assert response.status_code == 200
    assert "Successfully deleted" in response.json()["info"]

    # Verify first derived log is deleted but second remains
    response = await client.get(f"/v0/logs?project={project_name}", headers=HEADERS)
    assert response.status_code == 200
    logs = response.json()["logs"]

    for log in logs:
        assert "derived1" not in log["derived_entries"]
        if log["id"] in log_ids:  # Only check base logs that had derived entries
            assert "derived2" in log["derived_entries"]

    # Delete second derived log
    response = await _delete_derived_logs(client, project_name, derived_log_ids2)
    assert response.status_code == 200
    assert "Successfully deleted" in response.json()["info"]

    # Verify all derived logs are deleted
    response = await client.get(f"/v0/logs?project={project_name}", headers=HEADERS)
    assert response.status_code == 200
    logs = response.json()["logs"]
    for log in logs:
        assert len(log["derived_entries"]) == 0


@pytest.mark.anyio
async def test_delete_derived_logs_by_filter(client: AsyncClient):
    project_name = "test_delete_derived_by_filter"
    await _create_project(client, project_name)

    # Create base logs
    log_ids = []
    for i in range(2):
        response = await _create_log(client, project_name, entries={"a": i * 10})
        assert response.status_code == 200
        log_ids.append(response.json()[0])

    # Create derived log
    key = "derived1"
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
    derived_log_ids = response.json()["derived_log_ids"]
    assert len(derived_log_ids) == 2

    # Delete specific derived log by filter
    response = await _delete_derived_logs(
        client,
        project_name,
        {"from_fields": "derived1"},
    )
    assert response.status_code == 200
    assert "Successfully deleted" in response.json()["info"]

    response = await client.get(f"/v0/logs?project={project_name}", headers=HEADERS)
    assert response.status_code == 200
    logs = response.json()["logs"]

    for log in logs:
        if log["id"] == log_ids[0]:
            assert "derived1" not in log["derived_entries"]
        elif log["id"] == log_ids[1]:
            assert "derived1" not in log["derived_entries"]


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
    await _create_several_logs(client, project_name)

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

    # 5) Test context
    resp = await client.get(
        "/v0/logs",
        params={
            "project": project_name,
            "context": "_/",
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
async def test_create_log_w_image(client: AsyncClient):
    project_name = "eval-project"
    _ = await _create_project(client, project_name)

    img_path = os.path.join(
        os.path.dirname(os.path.realpath(__file__)),
        "sample_datasets/img.png",
    )
    success, buffer = cv2.imencode(".png", cv2.imread(img_path))
    assert success
    img = base64.b64encode(buffer).decode("utf-8")

    # log image
    response = await _create_log(
        client,
        project_name,
        params={},
        entries={
            "img_raw": img,
            "img_url": "https://upload.wikimedia.org/wikipedia/commons/4/45/Eopsaltria_australis_-_Mogo_Campground.jpg",
        },
    )

    assert response.status_code == 200, response.json()
    assert isinstance(response.json()[0], int)

    # Verify field type
    field_types_response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert field_types_response.status_code == 200
    assert field_types_response.json() == {
        "img_raw": {
            "data_type": "image",
            "field_type": "entry",
            "mutable": True,
            "artifacts": "",
        },
        "img_url": {
            "data_type": "image",
            "field_type": "entry",
            "mutable": True,
            "artifacts": "",
        },
    }


@pytest.mark.anyio
async def test_create_logs_autoincrement_version(client: AsyncClient):
    project_name = "non-matching-versions"
    _ = await _create_project(client, project_name)

    # This should work fine
    response = await client.post(
        "/v0/logs",
        json={"project": project_name, "params": {"p1": "test"}},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # same version and value
    response = await client.post(
        "/v0/logs",
        json={"project": project_name, "params": {"p1": "test"}},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # same version and different value -> autoincrement
    response = await client.post(
        "/v0/logs",
        json={"project": project_name, "params": {"p1": "test_v1"}},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()


@pytest.mark.anyio
async def test_create_logs_project_not_found(client: AsyncClient):
    project_name = "non_existent_project"

    response = await _create_log(client, project_name)

    assert response.status_code == 404, response.json()
    assert response.json() == {"detail": "Project not found."}


@pytest.mark.anyio
async def test_update_logs_overwrites(client: AsyncClient):
    project_name = "eval-project"
    _ = await _create_project(client, project_name)

    response = await _create_log(client, project_name, entries=log_data["log"])
    assert response.status_code == 200, response.json()
    log_id = response.json()[0]

    response = await _get_log(client, project_name, log_id)
    assert response.status_code == 200, response.json()
    orig_entries = response.json()["logs"][0]["entries"]
    assert len(orig_entries) == 3

    response = await client.post(
        "/v0/logs",
        json={"project": project_name, "entries": log_data["log_update"]},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    log_id_2 = response.json()[0]

    log_ids = [log_id, log_id_2]

    response = await _update_multiple_logs_w_overwrite(client, log_ids, overwrite=False)
    assert response.status_code == 400, response.json()

    response = await _update_multiple_logs_w_overwrite(client, log_ids, overwrite=True)
    assert response.status_code == 200, response.json()

    response = await _get_log(client, project_name, log_id)
    assert response.status_code == 200, response.json()
    new_entries = response.json()["logs"][0]["entries"]
    assert len(new_entries) == 3
    assert new_entries["a/b/c/input"] == orig_entries["a/b/c/input"]
    assert new_entries["a/b/c/boolean_input"] != orig_entries["a/b/c/boolean_input"]
    assert new_entries["a/b/c/numeric_input"] != orig_entries["a/b/c/numeric_input"]

    response = await _get_log(client, project_name, log_id_2)
    assert response.status_code == 200, response.json()
    new_entries = response.json()["logs"][0]["entries"]
    assert len(new_entries) == 4


@pytest.mark.anyio
async def test_delete_project_deletes_logs(client: AsyncClient):
    url = "/v0/project/test-project"
    project_name = "test-project"

    # Create a project first to delete it
    # check that existing projects don't change the functionality
    _ = await _create_project(client, project_name, user=2)
    create_response = await _create_project(client, project_name)
    assert create_response.status_code == 200

    # add a log
    response = await _create_log(client, project_name)
    assert response.status_code == 200, response.json()
    log_id = response.json()[0]
    assert isinstance(log_id, int)

    # verify it exists
    response = await _get_log(client, project_name, log_id)
    assert response.status_code == 200, response.json()

    # Now delete the project
    response = await client.delete(url, headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["info"] == "Project deleted successfully"

    # Verify the log has gone
    response = await _get_log(client, project_name, log_id)
    assert response.status_code == 404, response.json()
    assert response.json() == {
        "detail": f"Project {project_name} not found.",
    }


@pytest.mark.anyio
async def test_get_log(client: AsyncClient):
    project_name = "eval-project"
    _ = await _create_project(client, project_name)
    log_response = await _create_log(client, project_name)
    log_id = log_response.json()[0]

    # fetch the log
    response = await _get_log(client, project_name, log_id)

    assert response.status_code == 200, response.json()
    assert "logs" in response.json()
    assert "params" in response.json()
    assert isinstance(response.json()["params"]["a/b/param1"]["0"], str)
    assert len(response.json()["logs"]) == 1
    assert isinstance(response.json()["logs"][0]["ts"], str)
    assert isinstance(response.json()["logs"][0]["params"]["a/b/param1"], str)
    assert isinstance(
        response.json()["logs"][0]["entries"]["a/b/c/boolean_input"],
        bool,
    )
    assert isinstance(
        response.json()["logs"][0]["entries"]["a/b/c/numeric_input"],
        float,
    )


@pytest.mark.parametrize(
    "expression, values",
    [
        (
            "((a == 5) and (b > 7)) or (len(c) < 10 and 'earth' not in d)",
            {"a": 5, "b": 8, "c": "abcdef", "d": "hello world"},
        ),
        (
            "submarine == 6.45 and van is False or len(ship) < 10 and 'audi' in car",
            {"submarine": 7.89, "van": True, "ship": "_" * 10, "car": "porsche"},
        ),
        (
            "coffee == 'hot' or ice_cream == 'cold' and temperature == 1.23",
            {"coffee": "hot", "ice_cream": "cold", "temperature": 1.23},
        ),
        (  # This needs to be the string from a json.dumps of a python object
            '(messages == [{"role": "assistant", '
            '"context": "you are a helpful assistant"}])',
            {
                "messages": [
                    {
                        "role": "assistant",
                        "context": "you are a helpful assistant",
                    },
                ],
            },
        ),
        (
            "exists(lorry)",
            {
                "lorry": "big",
            },
        ),
        (
            "exists(car)",
            {
                "lorry": "big",
            },
        ),
        (
            "not exists(car)",
            {
                "lorry": "big",
            },
        ),
        ('a == "\'"', {"a": "'"}),
        ("a == '\\\"'", {"a": '"'}),
        ("a == '\\\\'", {"a": "\\"}),
        ('a == "He said, \\"Hello\\""', {"a": 'He said, "Hello"'}),
        ("a == 'It\\'s a test'", {"a": "It's a test"}),
    ],
)
async def test_log_filter_helper(client: AsyncClient, expression, values):

    project_name = "test_filter_helper"
    _ = await _create_project(client, project_name, user=1)
    response = await client.post(
        "/v0/logs",
        json={"project": project_name, "entries": values},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    response = await client.get(
        "/v0/logs",
        params={"project": project_name, "filter_expr": expression},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    result = len(response.json()["logs"]) == 1
    for key, value in values.items():
        exec(key + "=" + (str(value) if isinstance(value, bool) else json.dumps(value)))
    if "not exists" in expression:
        expected = expression.split("exists(")[-1].split(")")[0] not in values
    elif "exists" in expression:
        expected = expression.split("exists(")[-1].split(")")[0] in values
    else:
        expected = eval(expression)
    assert result == expected


@pytest.mark.parametrize(
    "expression, values",
    [
        # Arithmetic
        ("(a + b) > 10", {"a": 5, "b": 8}),
        ("(a - b) == 2", {"a": 5, "b": 3}),
        ("(a * b) == 15", {"a": 3, "b": 5}),
        ("(a / b) == 2", {"a": 10, "b": 5}),
        ("(a % b) == 1", {"a": 10, "b": 3}),
        # Logical
        ("(a > 5) and (b < 10)", {"a": 6, "b": 9}),
        ("(a < 5) or (b > 10)", {"a": 4, "b": 11}),
        ("not (a == 5)", {"a": 4}),
        # Comparison
        ("a == 5", {"a": 5}),
        ("a != 5", {"a": 4}),
        ("a < 5", {"a": 4}),
        ("a > 5", {"a": 6}),
        ("a <= 5", {"a": 5}),
        ("a >= 5", {"a": 5}),
        # Membership
        ("a in [1, 2, 3]", {"a": 2}),
        ("a not in [1, 2, 3]", {"a": 4}),
        # Indexing + Rounding
        ("round(x['some_key'], 2) >= 100.44", {"x": {"some_key": 100.4479}}),
        (
            "round_timestamp(x['_timestamp'], 5) == '1993-03-23T00:00:02+00:00'",
            {
                "x": {
                    "_timestamp": datetime(
                        1993,
                        3,
                        23,
                        0,
                        0,
                        3,
                        tzinfo=timezone.utc,
                    ).isoformat(),
                },
            },
        ),
        # Round to nearest 5 seconds - should round down
        (
            "round_timestamp(x['_timestamp'], 5) == '1993-03-23T00:00:00+00:00'",
            {
                "x": {
                    "_timestamp": datetime(
                        1993,
                        3,
                        23,
                        0,
                        0,
                        2,
                        tzinfo=timezone.utc,
                    ).isoformat(),
                },
            },
        ),
        # Round to nearest minute (60 seconds)
        (
            "round_timestamp(x['_timestamp'], 60) == '1993-03-23T00:00:00+00:00'",
            {
                "x": {
                    "_timestamp": datetime(
                        1993,
                        3,
                        23,
                        0,
                        0,
                        29,
                        tzinfo=timezone.utc,
                    ).isoformat(),
                },
            },
        ),
        # Round to nearest 15 minutes (900 seconds)
        (
            "round_timestamp(x['_timestamp'], 900) == '1993-03-23T00:15:00+00:00'",
            {
                "x": {
                    "_timestamp": datetime(
                        1993,
                        3,
                        23,
                        0,
                        8,
                        0,
                        tzinfo=timezone.utc,
                    ).isoformat(),
                },
            },
        ),
        (
            "x['timestamps'][0]['time1'] >= '1993-03-25T00:00:00+00:00'",
            {
                "x": {
                    "timestamps": [
                        {
                            "time1": (
                                datetime(1993, 3, 24, tzinfo=timezone.utc)
                            ).isoformat(),
                        },
                        {
                            "time2": (
                                datetime(1993, 3, 27, tzinfo=timezone.utc)
                            ).isoformat(),
                        },
                    ],
                },
            },
        ),
        # Nested Logical and Arithmetic
        ("((a + b) > 10) and ((c * d) < 20)", {"a": 5, "b": 8, "c": 2, "d": 3}),
        ("((a - b) == 2) or ((e / f) == 3)", {"a": 5, "b": 3, "e": 9, "f": 3}),
        # More Complex Nested Expressions
        ("(len(a) == 3) and ((b + c) > 10)", {"a": [1, 2, 3], "b": 5, "c": 6}),
        ("(to_str(a) == 'abc') or (len(b) == 2)", {"a": "abc", "b": [1, 2]}),
        # Using exists with nested conditions
        ("exists(a) and (b > 5)", {"a": 5, "b": 6}),
        ("not exists(c) or (d < 10)", {"d": 9}),
    ],
)
async def test_log_filter_helper_w_arithmetic(client: AsyncClient, expression, values):

    project_name = "test_filter_helper"
    _ = await _create_project(client, project_name, user=1)
    response = await client.post(
        "/v0/logs",
        json={"project": project_name, "entries": values},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    response = await client.get(
        "/v0/logs",
        params={"project": project_name, "filter_expr": expression},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    result = len(response.json()["logs"]) == 1
    for key, value in values.items():
        exec(key + "=" + (str(value) if isinstance(value, bool) else json.dumps(value)))

    # Replace to_str with str in the expression for evaluation
    eval_expression = expression.replace("to_str", "str")

    # Handle exists checks
    if "not exists" in eval_expression:
        expected = eval_expression.split("exists(")[-1].split(")")[0] not in values
    elif "exists" in eval_expression:
        expected = eval_expression.split("exists(")[-1].split(")")[0] in values
    elif "round_timestamp" in eval_expression:
        ts_expr, sec_expr = (
            eval_expression.split("round_timestamp(")[-1].split(")")[0].split(",")
        )
        ts_expr = ts_expr.strip()
        sec_expr = int(sec_expr.strip())
        ts_value = datetime.fromisoformat(eval(ts_expr))
        rounded_ts = datetime.fromtimestamp(
            round(ts_value.timestamp() / sec_expr) * sec_expr,
            tz=ts_value.tzinfo,
        )
        rounded_ts_iso = rounded_ts.isoformat()
        eval_expression = eval_expression.replace(
            "round_timestamp({}, {})".format(ts_expr, sec_expr),
            "'{}'".format(rounded_ts_iso),
        )
        expected = eval(eval_expression)
    else:
        expected = eval(eval_expression)

    assert result == expected


@pytest.mark.anyio
async def test_get_logs_with_derived_math_expressions_and_indexing(client: AsyncClient):

    project_name = "test_derived_logs_math"
    user_id = 1

    # 1) Create project
    await _create_project(client, project_name, user=user_id)

    # 2) Create the base logs (7 logs total).
    await _create_several_logs(client, project_name, user=user_id)

    # Fetch them back to confirm we have 7 log events.
    resp = await client.get(
        "/v0/logs",
        params={"project": project_name, "sorting": json.dumps({"id": "ascending"})},
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    base_logs = data["logs"]
    assert (
        len(base_logs) == 7
    ), f"Expected exactly 7 logs from _create_several_logs, got {len(base_logs)}"

    # Let's locate logs by description (and track the one missing description).
    log_id_boiling = None
    log_id_freezing = None
    log_id_sun = None
    log_id_nitrogen = None
    log_id_lava = None
    log_id_air = None
    log_id_no_desc = None

    for log_obj in base_logs:
        desc = log_obj["entries"].get("_/description", "")
        _id = log_obj["id"]
        if desc == "boiling water":
            log_id_boiling = _id
        elif desc == "freezing water":
            log_id_freezing = _id
        elif desc == "surface of the sun":
            log_id_sun = _id
        elif desc == "freezing nitrogen":
            log_id_nitrogen = _id
        elif desc == "lava":
            log_id_lava = _id
        elif desc == "air":
            log_id_air = _id
        else:
            log_id_no_desc = _id

    # Sanity-check that we found all 7
    assert all(
        [
            log_id_boiling,
            log_id_freezing,
            log_id_sun,
            log_id_nitrogen,
            log_id_lava,
            log_id_air,
            log_id_no_desc,
        ],
    ), "Did not locate all 7 logs by description / no-desc."

    ############################################################
    #              3) Create Derived Logs
    ############################################################

    #
    # (A) Add 10 to _/temperature for logs [boiling, freezing, sun]
    #
    derived_conf_add10 = {
        "key": "dl_add10",
        "equation": "{temp:_/temperature} + 10",
        "referenced_logs": {
            "temp": [log_id_boiling, log_id_freezing, log_id_sun],
        },
    }
    resp = await _create_derived_entry(
        client,
        project_name,
        derived_conf_add10["key"],
        derived_conf_add10["equation"],
        derived_conf_add10["referenced_logs"],
        user=user_id,
    )
    assert resp.status_code == 200, resp.json()
    dl_add10_ids = resp.json()["derived_log_ids"]
    assert len(dl_add10_ids) == 3, f"Expected 3 derived logs, got {dl_add10_ids}"

    #
    # (B) Convert Celsius→Fahrenheit: (C × 9/5) + 32, referencing [boiling, freezing]
    #
    derived_conf_c_to_f = {
        "key": "dl_c_to_f",
        "equation": "({C:_/temperature} * 9 / 5) + 32",
        "referenced_logs": {
            "C": [log_id_boiling, log_id_freezing],
        },
    }
    resp = await _create_derived_entry(
        client,
        project_name,
        derived_conf_c_to_f["key"],
        derived_conf_c_to_f["equation"],
        derived_conf_c_to_f["referenced_logs"],
        user=user_id,
    )
    assert resp.status_code == 200, resp.json()
    dl_c_to_f_ids = resp.json()["derived_log_ids"]
    assert len(dl_c_to_f_ids) == 2, "Only boiling & freezing logs used"

    #
    # (C) Round the temperature to nearest hundred: round({t:_/temperature}, -2)
    #     We'll reference [boiling, freezing, sun, nitrogen] for variety.
    #
    derived_conf_round_temp = {
        "key": "dl_round_temp",
        "equation": "round({t:_/temperature}, -2)",
        "referenced_logs": {
            "t": [log_id_boiling, log_id_freezing, log_id_sun, log_id_nitrogen],
        },
    }
    resp = await _create_derived_entry(
        client,
        project_name,
        derived_conf_round_temp["key"],
        derived_conf_round_temp["equation"],
        derived_conf_round_temp["referenced_logs"],
        user=user_id,
    )
    assert resp.status_code == 200, resp.json()
    dl_round_temp_ids = resp.json()["derived_log_ids"]
    assert len(dl_round_temp_ids) == 4

    #
    # (D) len({desc:_/description}) for [all logs that have _/description].
    #     That excludes the log with no description (log_id_no_desc).
    #
    logs_with_desc = [
        log_id_boiling,
        log_id_freezing,
        log_id_sun,
        log_id_nitrogen,
        log_id_lava,
        log_id_air,
    ]
    derived_conf_len_desc = {
        "key": "dl_len_desc",
        "equation": "len({desc:_/description})",
        "referenced_logs": {"desc": logs_with_desc},
    }
    resp = await _create_derived_entry(
        client,
        project_name,
        derived_conf_len_desc["key"],
        derived_conf_len_desc["equation"],
        derived_conf_len_desc["referenced_logs"],
        user=user_id,
    )
    assert resp.status_code == 200, resp.json()
    dl_len_desc_ids = resp.json()["derived_log_ids"]
    assert len(dl_len_desc_ids) == len(logs_with_desc)

    #
    # (E) Subtraction across logs: "Sun temp minus boiling temp"
    #
    derived_conf_sub = {
        "key": "dl_sun_minus_boil",
        "equation": "{sun:_/temperature} - {boil:_/temperature}",
        "referenced_logs": {
            "sun": [log_id_sun],
            "boil": [log_id_boiling],
        },
    }
    resp = await _create_derived_entry(
        client,
        project_name,
        derived_conf_sub["key"],
        derived_conf_sub["equation"],
        derived_conf_sub["referenced_logs"],
        user=user_id,
    )
    assert resp.status_code == 200, resp.json()
    dl_sub_ids = resp.json()["derived_log_ids"]
    assert len(dl_sub_ids) >= 1, "Should create derived log for that combination"

    #
    # (F) Indexing a list: {m:_/metadata}[1] + 2
    #     We'll reference logs known to have _/metadata = [1,5,6] (lava) and [3,8,5] (air).
    #     (We won't include nitrogen etc. if they don't have _/metadata.)
    #
    derived_conf_index_array = {
        "key": "dl_index_array",
        "equation": "{m:_/metadata}[1] + 2",
        "referenced_logs": {
            "m": [log_id_lava, log_id_air],  # they both have _/metadata
        },
    }
    resp = await _create_derived_entry(
        client,
        project_name,
        derived_conf_index_array["key"],
        derived_conf_index_array["equation"],
        derived_conf_index_array["referenced_logs"],
        user=user_id,
    )
    assert resp.status_code == 200, resp.json()
    dl_index_array_ids = resp.json()["derived_log_ids"]
    assert len(dl_index_array_ids) == 2, "lava + air"

    #
    # (G) Indexing a dict: {d:_/_data}['b'] + 5
    #     We'll reference logs #5 (lava => b=4), #6 (air => b=12), #7 (no desc => b=10).
    #
    derived_conf_index_dict = {
        "key": "dl_index_dict",
        "equation": "{d:_/_data}['b'] + 5",
        "referenced_logs": {
            "d": [log_id_lava, log_id_air, log_id_no_desc],
        },
    }
    resp = await _create_derived_entry(
        client,
        project_name,
        derived_conf_index_dict["key"],
        derived_conf_index_dict["equation"],
        derived_conf_index_dict["referenced_logs"],
        user=user_id,
    )
    assert resp.status_code == 200, resp.json()
    dl_index_dict_ids = resp.json()["derived_log_ids"]
    assert len(dl_index_dict_ids) == 3, "lava + air + no-desc"

    # (H) Exponent: e.g. {sun:_/temperature} ** 2
    derived_conf_exp = {
        "key": "dl_sun_exp2",
        "equation": "{sun:_/temperature} ** 2",
        "referenced_logs": {
            "sun": [log_id_sun],  # surface of sun = 6000
        },
    }
    resp = await _create_derived_entry(
        client,
        project_name,
        derived_conf_exp["key"],
        derived_conf_exp["equation"],
        derived_conf_exp["referenced_logs"],
        user=user_id,
    )
    assert resp.status_code == 200, resp.json()
    dl_exp_ids = resp.json()["derived_log_ids"]
    assert len(dl_exp_ids) == 1

    # (I) Floor Division: e.g. {boil:_/temperature} // 3
    derived_conf_floor_div = {
        "key": "dl_boil_floor_div",
        "equation": "{boil:_/temperature} // 3",
        "referenced_logs": {
            "boil": [log_id_boiling],
        },
    }
    resp = await _create_derived_entry(
        client,
        project_name,
        derived_conf_floor_div["key"],
        derived_conf_floor_div["equation"],
        derived_conf_floor_div["referenced_logs"],
        user=user_id,
    )
    assert resp.status_code == 200, resp.json()
    dl_floor_div_ids = resp.json()["derived_log_ids"]
    assert len(dl_floor_div_ids) == 1

    ############################################################################
    # 4) Verify the derived entries in GET /v0/logs
    ############################################################################

    resp = await client.get(
        "/v0/logs",
        params={"project": project_name, "sorting": json.dumps({"id": "ascending"})},
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    data_all = resp.json()
    all_logs = data_all["logs"]
    assert len(all_logs) == 7, "Should still be 7 logs in this project."

    # We'll check each log_event for the derived values
    for log_obj in all_logs:
        log_id = log_obj["id"]
        entries = log_obj["entries"]
        derived = log_obj.get("derived_entries", {})

        # Unpack some known fields
        temp = entries.get("_/temperature")
        desc = entries.get("_/description", "")
        metadata = entries.get("_/metadata")
        data_dict = entries.get("_/_data")

        # (A) dl_add10 => temp + 10
        add10_val = derived.get("dl_add10")
        if add10_val is not None and temp is not None:
            expected = temp + 10
            assert (
                abs(add10_val - expected) < 1e-7
            ), f"dl_add10 mismatch: log_id={log_id}, got {add10_val}, expected {expected}"

        # (B) dl_c_to_f => (temp * 9/5) + 32
        c_to_f_val = derived.get("dl_c_to_f")
        if c_to_f_val is not None and temp is not None:
            expected = (temp * 9.0 / 5.0) + 32
            assert (
                abs(c_to_f_val - expected) < 1e-7
            ), f"dl_c_to_f mismatch: log_id={log_id}, got {c_to_f_val}, expected {expected}"

        # (C) dl_round_temp => round(temp, -2)
        rtemp_val = derived.get("dl_round_temp")
        if rtemp_val is not None and temp is not None:
            # For example,  100 => 100, 0 => 0, 6000 => 6000, -210 => -200
            expected = round(temp, -2)
            assert (
                rtemp_val == expected
            ), f"round_temp mismatch: log_id={log_id}, got {rtemp_val}, expected {expected}"

        # (D) dl_len_desc => len(desc)
        len_desc_val = derived.get("dl_len_desc")
        if len_desc_val is not None:
            expected_len = len(desc)
            assert (
                len_desc_val == expected_len
            ), f"dl_len_desc mismatch: log_id={log_id}, got {len_desc_val}, expected {expected_len}"

        # (E) dl_sun_minus_boil => (sun_temp - boil_temp)
        sub_val = derived.get("dl_sun_minus_boil")
        # Typically only the "sun" log would have a valid numeric result; "boiling" might see None
        if sub_val is not None and log_id == log_id_sun and temp is not None:
            # sun=6000, boil=100 => 5900
            # (assuming these are still the original temperatures)
            expected = 6000 - 100
            assert (
                abs(sub_val - expected) < 1e-7
            ), f"Expected sun-boil=5900 on log_id={log_id}, got {sub_val}"

        # (F) dl_index_array => {m:_/metadata}[1] + 2
        index_array_val = derived.get("dl_index_array")
        if index_array_val is not None and metadata:
            # For "lava" => metadata=[1,5,6], [1] => 5 => +2 => 7
            # For "air"  => metadata=[3,8,5], [1] => 8 => +2 => 10
            expected = metadata[1] + 2
            assert (
                index_array_val == expected
            ), f"dl_index_array mismatch: log_id={log_id}, got {index_array_val}, expected {expected}"

        # (G) dl_index_dict => {d:_/_data}['b'] + 5
        index_dict_val = derived.get("dl_index_dict")
        if index_dict_val is not None and data_dict and "b" in data_dict:
            # For lava => b=4 => +5 => 9
            # For air  => b=12 => +5 => 17
            # For no_desc => b=10 => +5 => 15
            expected = data_dict["b"] + 5
            assert (
                index_dict_val == expected
            ), f"dl_index_dict mismatch: log_id={log_id}, got {index_dict_val}, expected {expected}"

        # (H) Check dl_sun_exp2 => 6000 ** 2 = 36,000,000
        sun_exp2_val = derived.get("dl_sun_exp2")
        if sun_exp2_val is not None and log_id == log_id_sun:
            expected = 6000**2
            assert (
                abs(sun_exp2_val - expected) < 1e-7
            ), f"Exponent mismatch on log_id={log_id}. Got {sun_exp2_val}, expected {expected}"

        # (I) Check dl_boil_floor_div => 100 // 3 = 33
        boil_floor_val = derived.get("dl_boil_floor_div")
        if boil_floor_val is not None and log_id == log_id_boiling:
            # 100 // 3 => 33 in Python
            expected = 33
            assert (
                boil_floor_val == expected
            ), f"Floor division mismatch on log_id={log_id}. Got {boil_floor_val}, expected {expected}"


@pytest.mark.anyio
async def test_filtering_and_sorting_base_and_derived_logs(client: AsyncClient):
    project_name = "test_base_derived_filters"
    user_id = 1

    await _create_project(client, project_name, user=user_id)

    base_logs_data = [
        {
            "entries": {
                "alpha/num": 100,
                "alpha/str": "hello",
                "common_field": True,
            },
            "params": {"p/param1": "base1-param"},
        },
        {
            "entries": {
                "beta/num": 5,
                "beta/str": "world",
                "common_field": False,
            },
            "params": {"p/param1": "base2-param"},
        },
    ]

    base_log_ids = []

    for data in base_logs_data:
        resp = await client.post(
            "/v0/logs",
            headers=HEADERS,
            json={
                "project": project_name,
                "entries": data["entries"],
                "params": data["params"],
            },
        )
        assert resp.status_code == 200, resp.json()
        out_data = resp.json()
        created_log_id = out_data[0]
        base_log_ids.append(created_log_id)

    assert len(base_log_ids) == 2, f"Expected 2 base log_event_ids, got {base_log_ids}"

    derived_definitions = [
        {
            "key": "derv/calcA",
            "equation": "{val:alpha/num} + 10",
            "referenced_logs": {"val": [base_log_ids[0]]},
        },
        {
            "key": "derv/calcB",
            "equation": "{val:beta/num} * 2",
            "referenced_logs": {"val": [base_log_ids[1]]},
        },
    ]

    derived_log_ids = []
    for ddef in derived_definitions:
        resp = await _create_derived_entry(
            client,
            project_name,
            key=ddef["key"],
            equation=ddef["equation"],
            referenced_logs=ddef["referenced_logs"],
            user=user_id,
        )
        assert resp.status_code == 200, resp.json()
        created_d_ids = resp.json()["derived_log_ids"]
        derived_log_ids.extend(created_d_ids)

    assert len(derived_log_ids) == 2, f"Expected 2 derived logs, got {derived_log_ids}"

    # (a) Test that *all* 2 base + 2 derived logs appear across 2 distinct log_event_ids
    logs_all = await fetch_logs(client, project_name)
    assert len(logs_all) == 2, "We created 2 distinct log events total."
    for log_obj in logs_all:
        log_id = log_obj["id"]
        if log_id == base_log_ids[0]:
            assert "alpha/num" in log_obj["entries"]
            assert "alpha/str" in log_obj["entries"]
            assert "derv/calcA" in log_obj["derived_entries"]
        elif log_id == base_log_ids[1]:
            assert "beta/num" in log_obj["entries"]
            assert "beta/str" in log_obj["entries"]
            assert "derv/calcB" in log_obj["derived_entries"]

    # (b) from_ids => If we only want log_id=base_log_ids[0], we should get 1 log event
    logs_single = await fetch_logs(client, project_name, from_ids=str(base_log_ids[0]))
    assert len(logs_single) == 1
    assert logs_single[0]["id"] == base_log_ids[0]
    assert "derv/calcA" in logs_single[0]["derived_entries"]

    # (c) exclude_ids => Exclude the second log_id => only the first remains
    logs_excluding = await fetch_logs(
        client,
        project_name,
        exclude_ids=str(base_log_ids[1]),
    )
    assert len(logs_excluding) == 1
    assert logs_excluding[0]["id"] == base_log_ids[0]

    # (d) from_fields => e.g. only keys that match ["alpha/num", "beta/num"].
    from_fields_param = "alpha/num&beta/num"
    logs_field_incl = await fetch_logs(
        client,
        project_name,
        from_fields=from_fields_param,
    )
    for lg in logs_field_incl:
        assert set(lg["entries"].keys()).issubset({"alpha/num", "beta/num"})
        assert lg["derived_entries"] == {}

    # (e) exclude_fields => e.g. exclude "common_field" from both logs + exclude "derv/calcB"
    exclude_fields_param = "common_field&derv/calcB"
    logs_excluding_fields = await fetch_logs(
        client,
        project_name,
        exclude_fields=exclude_fields_param,
    )
    for lg in logs_excluding_fields:
        assert "common_field" not in lg["entries"]
        assert "derv/calcB" not in lg["derived_entries"]
        if lg["id"] == base_log_ids[0]:
            assert "derv/calcA" in lg["derived_entries"]

    # (f) column_context => Suppose we only want logs with a key starting with "alpha/"
    col_ctx = "alpha/entries"
    logs_alpha = await fetch_logs(client, project_name, column_context=col_ctx)
    assert len(logs_alpha) == 1
    assert logs_alpha[0]["id"] == base_log_ids[0]
    assert set(logs_alpha[0]["entries"].keys()) == {"num", "str"}
    assert logs_alpha[0]["derived_entries"] == {}

    # (g) filter_expr => e.g. "alpha/num > 50 or beta/num < 10"
    logs_filtered = await fetch_logs(
        client,
        project_name,
        filter_expr="derv/calcA > 50 or derv/calcB <= 10",
    )
    assert len(logs_filtered) == 2, "Both logs match the filter expression."

    # (h) sorting => e.g. sort by alpha/num descending
    logs_sorted = await fetch_logs(
        client,
        project_name,
        sorting=json.dumps({"derv/calcA": "descending"}),
    )
    assert len(logs_sorted) == 2
    assert logs_sorted[0]["id"] == base_log_ids[0]


@pytest.mark.anyio
async def test_get_logs(client: AsyncClient):
    project_name = "eval-project"
    # create the same project with another user to ensure the correct one
    # is fetched
    _ = await _create_project(client, project_name, user=2)
    _ = await _create_project(client, project_name, user=1)
    _ = await _create_log(client, project_name, user=1)

    # fetch entries for the project
    response = await client.get(f"/v0/logs?project={project_name}", headers=HEADERS)

    assert response.status_code == 200, response.json()
    assert isinstance(response.json(), dict)
    assert isinstance(response.json()["params"], dict)
    assert isinstance(response.json()["params"]["a/b/param1"]["0"], str)
    assert isinstance(response.json()["logs"], list)
    assert isinstance(response.json()["logs"][0]["ts"], str)
    assert isinstance(
        response.json()["logs"][0]["entries"]["a/b/c/boolean_input"],
        bool,
    )
    assert isinstance(
        response.json()["logs"][0]["entries"]["a/b/c/numeric_input"],
        float,
    )
    assert isinstance(response.json()["logs"][0]["params"]["a/b/param1"], str)

    # assert the field ordering is correct
    assert (
        json.dumps([list(lg["entries"].keys()) for lg in response.json()["logs"]])
        == '[["a/b/c/input", "a/b/c/boolean_input", "a/b/c/numeric_input"]]'
    )

    # fetch entries for the empty project
    response = await client.get(f"/v0/logs?project={project_name}", headers=HEADERS_2)

    assert response.status_code == 200, response.json()
    assert isinstance(response.json()["logs"], list)
    assert len(response.json()["logs"]) == 0


@pytest.mark.anyio
async def test_get_params(client: AsyncClient):
    project_name = "eval-project"
    # create the same project with another user to ensure the correct one
    # is fetched
    _ = await _create_project(client, project_name)
    _ = await _create_log(client, project_name)

    # fetch all params for the project
    response = await client.get(
        f"/v0/logs?project={project_name}",
        headers=HEADERS,
        params={"column_context": "params"},
    )
    assert response.status_code == 200, response.json()
    assert isinstance(response.json(), dict)
    assert isinstance(response.json()["params"], dict)
    assert isinstance(response.json()["params"]["a/b/param1"]["0"], str)
    assert isinstance(response.json()["logs"], list)
    assert isinstance(response.json()["logs"][0]["ts"], str)
    assert response.json()["logs"][0]["entries"] == {}
    assert isinstance(response.json()["logs"][0]["params"]["a/b/param1"], str)

    # fetch params for the project with the full context, prepended by "params"
    response = await client.get(
        f"/v0/logs?project={project_name}",
        headers=HEADERS,
        params={"column_context": "params/a/b"},
    )
    assert response.status_code == 200, response.json()
    assert isinstance(response.json(), dict)
    assert isinstance(response.json()["params"], dict)
    assert isinstance(response.json()["params"]["param1"]["0"], str)
    assert isinstance(response.json()["logs"], list)
    assert isinstance(response.json()["logs"][0]["ts"], str)
    assert response.json()["logs"][0]["entries"] == {}
    assert isinstance(response.json()["logs"][0]["params"]["param1"], str)

    # fetch params for the project with the full context, with "params" inside
    response = await client.get(
        f"/v0/logs?project={project_name}",
        headers=HEADERS,
        params={"column_context": "a/params/b"},
    )
    assert response.status_code == 200, response.json()
    assert isinstance(response.json(), dict)
    assert isinstance(response.json()["params"], dict)
    assert isinstance(response.json()["params"]["param1"]["0"], str)
    assert isinstance(response.json()["logs"], list)
    assert isinstance(response.json()["logs"][0]["ts"], str)
    assert response.json()["logs"][0]["entries"] == {}
    assert isinstance(response.json()["logs"][0]["params"]["param1"], str)

    # fetch params for the project with the full context, appended by "params"
    response = await client.get(
        f"/v0/logs?project={project_name}",
        headers=HEADERS,
        params={"column_context": "a/b/params"},
    )
    assert response.status_code == 200, response.json()
    assert isinstance(response.json(), dict)
    assert isinstance(response.json()["params"], dict)
    assert isinstance(response.json()["params"]["param1"]["0"], str)
    assert isinstance(response.json()["logs"], list)
    assert isinstance(response.json()["logs"][0]["ts"], str)
    assert response.json()["logs"][0]["entries"] == {}
    assert isinstance(response.json()["logs"][0]["params"]["param1"], str)


@pytest.mark.anyio
async def test_get_entries(client: AsyncClient):
    project_name = "eval-project"
    # create the same project with another user to ensure the correct one
    # is fetched
    _ = await _create_project(client, project_name)
    _ = await _create_log(client, project_name)

    # fetch all entries for the project
    response = await client.get(
        f"/v0/logs?project={project_name}",
        headers=HEADERS,
        params={"column_context": "entries"},
    )

    assert response.status_code == 200, response.json()
    assert isinstance(response.json(), dict)
    assert isinstance(response.json()["params"], dict)
    assert response.json()["params"] == {}
    assert isinstance(response.json()["logs"], list)
    assert isinstance(response.json()["logs"][0]["ts"], str)
    assert isinstance(
        response.json()["logs"][0]["entries"]["a/b/c/boolean_input"],
        bool,
    )
    assert isinstance(
        response.json()["logs"][0]["entries"]["a/b/c/numeric_input"],
        float,
    )
    assert response.json()["logs"][0]["params"] == {}

    # fetch entries for the project with the full context, prepended by "entries"
    response = await client.get(
        f"/v0/logs?project={project_name}",
        headers=HEADERS,
        params={"column_context": "entries/a/b"},
    )

    assert response.status_code == 200, response.json()
    assert isinstance(response.json(), dict)
    assert isinstance(response.json()["params"], dict)
    assert response.json()["params"] == {}
    assert isinstance(response.json()["logs"], list)
    assert isinstance(response.json()["logs"][0]["ts"], str)
    assert isinstance(
        response.json()["logs"][0]["entries"]["c/boolean_input"],
        bool,
    )
    assert isinstance(
        response.json()["logs"][0]["entries"]["c/numeric_input"],
        float,
    )
    assert response.json()["logs"][0]["params"] == {}

    # fetch entries for the project with the full context, with "entries" inside
    response = await client.get(
        f"/v0/logs?project={project_name}",
        headers=HEADERS,
        params={"column_context": "a/entries/b"},
    )

    assert response.status_code == 200, response.json()
    assert isinstance(response.json(), dict)
    assert isinstance(response.json()["params"], dict)
    assert response.json()["params"] == {}
    assert isinstance(response.json()["logs"], list)
    assert isinstance(response.json()["logs"][0]["ts"], str)
    assert isinstance(
        response.json()["logs"][0]["entries"]["c/boolean_input"],
        bool,
    )
    assert isinstance(
        response.json()["logs"][0]["entries"]["c/numeric_input"],
        float,
    )
    assert response.json()["logs"][0]["params"] == {}

    # fetch entries for the project with the full context, appended by "entries"
    response = await client.get(
        f"/v0/logs?project={project_name}",
        headers=HEADERS,
        params={"column_context": "a/b/entries"},
    )

    assert response.status_code == 200, response.json()
    assert isinstance(response.json(), dict)
    assert isinstance(response.json()["params"], dict)
    assert response.json()["params"] == {}
    assert isinstance(response.json()["logs"], list)
    assert isinstance(response.json()["logs"][0]["ts"], str)
    assert isinstance(
        response.json()["logs"][0]["entries"]["c/boolean_input"],
        bool,
    )
    assert isinstance(
        response.json()["logs"][0]["entries"]["c/numeric_input"],
        float,
    )
    assert response.json()["logs"][0]["params"] == {}


@pytest.mark.anyio
async def test_get_logs_from_ids(client: AsyncClient):
    project_name = "eval-project"
    # create the same project with another user to ensure the correct one
    # is fetched
    _ = await _create_project(client, project_name)
    _ = await _create_several_logs(client, project_name)

    # fetch entries for the project
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"return_ids_only": True},
        headers=HEADERS,
    )
    ids = response.json()
    from_ids = ids[0:4]

    # fetch entries for the project
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"from_ids": "&".join([str(i) for i in from_ids])},
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    response = response.json()
    logs = response["logs"]
    assert len(logs) == 4
    assert len(logs) == response["count"]
    assert [log["id"] for log in logs] == [i for i in ids if i in from_ids]


@pytest.mark.anyio
async def test_get_logs_excluding_ids(client: AsyncClient):
    project_name = "eval-project"
    # create the same project with another user to ensure the correct one
    # is fetched
    _ = await _create_project(client, project_name)
    _ = await _create_several_logs(client, project_name)

    # fetch entries for the project
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"return_ids_only": True},
        headers=HEADERS,
    )
    ids = response.json()
    exclude_ids = ids[0:4]

    # fetch entries for the project
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"exclude_ids": "&".join([str(i) for i in exclude_ids])},
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    response = response.json()
    logs = response["logs"]
    assert len(logs) == 3
    assert len(logs) == response["count"]
    assert [log["id"] for log in logs] == [i for i in ids if i not in exclude_ids]


@pytest.mark.anyio
async def test_get_logs_from_fields(client: AsyncClient):
    project_name = "eval-project"
    # create the same project with another user to ensure the correct one
    # is fetched
    _ = await _create_project(client, project_name)
    _ = await _create_several_logs(client, project_name)

    # fetch entries for the project
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"from_fields": "&".join(["_/temperature", "_/state", "_/metadata"])},
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    response = response.json()
    logs = response["logs"]
    assert len(logs) == 6
    assert len(logs) == response["count"]
    assert logs[0]["entries"] == {"_/metadata": [3, 8, 5]}
    assert logs[1]["entries"] == {"_/metadata": [1, 5, 6]}
    assert logs[2]["entries"] == {"_/temperature": -210.0, "_/state": "liquid->solid"}
    assert logs[3]["entries"] == {"_/temperature": 6000.0, "_/state": "gas"}
    assert logs[4]["entries"] == {"_/temperature": 0.0, "_/state": "liquid->solid"}
    assert logs[5]["entries"] == {"_/temperature": 100.0, "_/state": "liquid->gas"}


@pytest.mark.anyio
async def test_get_logs_excluding_fields(client: AsyncClient):
    project_name = "eval-project"
    # create the same project with another user to ensure the correct one
    # is fetched
    _ = await _create_project(client, project_name)
    _ = await _create_several_logs(client, project_name)

    # fetch entries for the project
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={
            "exclude_fields": "&".join(
                [
                    "_/temperature",
                    "_/state",
                    "_/_data",
                    "_/timestamp",
                    "a/b/param1",
                ],
            ),
        },
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    response = response.json()
    logs = response["logs"]
    assert len(logs) == 6
    assert len(logs) == response["count"]
    assert logs[0]["entries"] == {"_/description": "air", "_/metadata": [3, 8, 5]}
    assert logs[1]["entries"] == {"_/description": "lava", "_/metadata": [1, 5, 6]}
    assert logs[2]["entries"] == {"_/description": "freezing nitrogen", "_/safe": False}
    assert logs[3]["entries"] == {
        "_/description": "surface of the sun",
        "_/safe": False,
    }
    assert logs[4]["entries"] == {"_/description": "freezing water", "_/safe": True}
    assert logs[5]["entries"] == {"_/description": "boiling water", "_/safe": False}


@pytest.mark.anyio
async def test_get_logs_w_column_context(client: AsyncClient):
    project_name = "eval-project"
    # create project and log
    _ = await _create_project(client, project_name, user=1)
    _ = await _create_log(client, project_name, user=1)

    # get full context log
    response = await client.get(
        f"/v0/logs?project={project_name}",
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    response = response.json()
    del response["logs"][0]["ts"]
    assert response == {
        "params": {
            "a/b/param1": {
                "0": "test",
            },
        },
        "logs": [
            {
                "id": 1,
                "entries": {
                    "a/b/c/input": "Some input data",
                    "a/b/c/boolean_input": True,
                    "a/b/c/numeric_input": 4.5,
                },
                "derived_entries": {},
                "clipped_fields": [],
                "params": {
                    "a/b/param1": "0",
                },
            },
        ],
        "count": 1,
    }

    # get log with "a" context
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"column_context": "a"},
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    response = response.json()
    del response["logs"][0]["ts"]
    assert response == {
        "params": {
            "b/param1": {
                "0": "test",
            },
        },
        "logs": [
            {
                "id": 1,
                "entries": {
                    "b/c/input": "Some input data",
                    "b/c/boolean_input": True,
                    "b/c/numeric_input": 4.5,
                },
                "derived_entries": {},
                "clipped_fields": [],
                "params": {
                    "b/param1": "0",
                },
            },
        ],
        "count": 1,
    }

    # get log with "a/b" context
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"column_context": "a/b"},
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    response = response.json()
    del response["logs"][0]["ts"]
    assert response == {
        "params": {
            "param1": {
                "0": "test",
            },
        },
        "logs": [
            {
                "id": 1,
                "entries": {
                    "c/input": "Some input data",
                    "c/boolean_input": True,
                    "c/numeric_input": 4.5,
                },
                "derived_entries": {},
                "clipped_fields": [],
                "params": {
                    "param1": "0",
                },
            },
        ],
        "count": 1,
    }

    # get log with "a/b/c" context
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"column_context": "a/b/c"},
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    response = response.json()
    del response["logs"][0]["ts"]
    assert response == {
        "params": {},
        "logs": [
            {
                "id": 1,
                "entries": {
                    "input": "Some input data",
                    "boolean_input": True,
                    "numeric_input": 4.5,
                },
                "derived_entries": {},
                "clipped_fields": [],
                "params": {},
            },
        ],
        "count": 1,
    }


@pytest.mark.anyio
async def test_get_logs_latest_timestamp(client: AsyncClient):

    # create logs
    project_name = "eval-project"
    _ = await _create_project(client, project_name, user=1)
    t0 = datetime.now(timezone.utc)
    _ = await _create_several_logs(client, project_name, user=1)

    # assert the latest timestamp t1 is more recent than t0
    response = await client.get(
        f"/v0/logs/latest_timestamp?project={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    response = response.json()
    assert isinstance(response, str)
    t1 = datetime.fromisoformat(response).replace(tzinfo=timezone.utc)
    assert t1 > t0

    # add new entries
    entries = {
        "new_entry": "Updated value",
        "explicit_types": {"new_entry": {"type": "str"}},
    }
    response = await _update_logs(client, [1, 2], entries)
    assert response.status_code == 200, response.json()
    assert response.json()["info"] == "Logs updated successfully!"

    # assert the latest timestamp t2 is more recent than t1
    response = await client.get(
        f"/v0/logs/latest_timestamp?project={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    response = response.json()
    assert isinstance(response, str)
    t2 = datetime.fromisoformat(response).replace(tzinfo=timezone.utc)
    assert t2 > t1


@pytest.mark.anyio
async def test_get_log_ids(client: AsyncClient):
    project_name = "eval-project"
    # create the same project with another user to ensure the correct one
    # is fetched
    _ = await _create_project(client, project_name, user=2)
    _ = await _create_project(client, project_name, user=1)
    _ = await _create_several_logs(client, project_name, user=1)

    # fetch entries for the project
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"return_ids_only": True},
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    response = response.json()
    assert isinstance(response, list)
    assert len(response) == 7
    correct = list(range(1, 8))
    correct.reverse()
    assert response == correct


@pytest.mark.anyio
async def test_get_logs_field_ordering(client: AsyncClient):
    project_name = "field-order-test"
    _ = await _create_project(client, project_name)

    # Create first log with fields in one order
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "entries": {
                "field1": "first",
                "field2": "second",
                "field3": "third",
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Create second log with fields in different order
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "entries": {
                "field2": "second again",
                "field3": "third again",
                "field1": "first again",
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Get logs and verify field ordering
    response = await client.get(
        f"/v0/logs?project={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200

    logs = response.json()["logs"]
    assert len(logs) == 2

    # Verify field ordering in first log
    first_log_fields = list(logs[0]["entries"].keys())
    assert first_log_fields == ["field1", "field2", "field3"]

    # Verify same field ordering in second log
    second_log_fields = list(logs[1]["entries"].keys())
    assert second_log_fields == ["field1", "field2", "field3"]


@pytest.mark.anyio
async def test_get_logs_with_value_limit(client: AsyncClient):
    project_name = "value-limit-test"
    _ = await _create_project(client, project_name)

    # Create test data with various value types and lengths
    test_data = {
        "entries": {
            "numeric_int": 12345,
            "numeric_float": 123.45,
            "short_string": "Hello",
            "long_string": "A" * 200,
            "nested_dict": {"key1": "value1", "key2": "value2"},
            "nested_list": [1, 2, 3, 4, 5],
            "nested_tuple": ("a", "b", "c"),
            "boolean_value": True,
        },
    }

    # Add image data
    img_path = os.path.join(
        os.path.dirname(os.path.realpath(__file__)),
        "sample_datasets/img.png",
    )
    success, buffer = cv2.imencode(".png", cv2.imread(img_path))
    assert success
    test_data["entries"]["image_field"] = base64.b64encode(buffer).decode("utf-8")

    # Create log with test data
    response = await client.post(
        "/v0/logs",
        json={"project": project_name, "entries": test_data["entries"]},
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Test with value_limit=10
    response = await client.get(
        f"/v0/logs?project={project_name}&value_limit=10",
        headers=HEADERS,
    )
    assert response.status_code == 200
    result = response.json()
    log_entries = result["logs"][0]["entries"]

    # Test numeric values remain unchanged
    assert log_entries["numeric_int"] == 12345
    assert log_entries["numeric_float"] == 123.45
    assert log_entries["boolean_value"] == True

    # Test string truncation
    assert log_entries["short_string"] == "Hello"  # Should not be truncated
    assert log_entries["long_string"] == "AAAAAAAAAA..."  # Should be truncated

    # Test nested structure handling
    assert len(log_entries["nested_dict"]) <= 13  # '{"key1":"va...'
    assert "..." in log_entries["nested_dict"]
    assert len(log_entries["nested_list"]) <= 13
    assert "..." in log_entries["nested_list"]
    assert len(log_entries["nested_tuple"]) <= 13
    assert "..." in log_entries["nested_tuple"]

    # Test clipping indicator
    assert "clipped_fields" in result["logs"][0]
    clipped_fields = result["logs"][0]["clipped_fields"]
    assert "long_string" in clipped_fields
    assert "nested_dict" in clipped_fields
    assert "nested_list" in clipped_fields
    assert "nested_tuple" in clipped_fields
    assert "image_field" in clipped_fields
    assert "short_string" not in clipped_fields
    assert "numeric_int" not in clipped_fields
    assert "numeric_float" not in clipped_fields
    assert "boolean_value" not in clipped_fields

    # Test with no value_limit (backward compatibility)
    response = await client.get(
        f"/v0/logs?project={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    result = response.json()
    log_entries = result["logs"][0]["entries"]

    # Verify all values are returned in full
    assert log_entries["numeric_int"] == 12345
    assert log_entries["numeric_float"] == 123.45
    assert log_entries["short_string"] == "Hello"
    assert log_entries["long_string"] == "A" * 200
    assert log_entries["nested_dict"] == {"key1": "value1", "key2": "value2"}
    assert log_entries["nested_list"] == [1, 2, 3, 4, 5]
    assert log_entries["nested_tuple"] == ["a", "b", "c"]  # JSON converts tuple to list
    assert log_entries["boolean_value"] == True

    # Verify no clipping indicators when value_limit is not set
    assert "clipped_fields" in result["logs"][0]
    assert len(result["logs"][0]["clipped_fields"]) == 0

    # Test with zero value_limit
    response = await client.get(
        f"/v0/logs?project={project_name}&value_limit=0",
        headers=HEADERS,
    )
    assert response.status_code == 200
    result = response.json()
    log_entries = result["logs"][0]["entries"]

    # Verify numeric values are unchanged but strings are empty or truncated
    assert log_entries["numeric_int"] == 12345
    assert log_entries["numeric_float"] == 123.45
    assert log_entries["boolean_value"] == True
    assert log_entries["short_string"] == "..."
    assert log_entries["long_string"] == "..."
    assert log_entries["nested_dict"] == "..."
    assert log_entries["nested_list"] == "..."
    assert log_entries["nested_tuple"] == "..."
    assert log_entries["image_field"] == ""


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
    _ = await _delete_logs(client, [([1, 2, 3, 4], None)])
    response = await client.get(
        f"/v0/logs?project={project_name}&group_threshold=1",
        headers=HEADERS,
    )
    assert response.status_code == 200
    result = response.json()
    assert len(result["logs"]) == 0
    assert "grouped_entries" not in result


@pytest.mark.anyio
async def test_get_empty_logs(client: AsyncClient):
    project_name = "eval-project"
    _ = await _create_project(client, project_name)

    # fetch entries for the project
    response = await client.get(f"/v0/logs?project={project_name}", headers=HEADERS)

    assert response.status_code == 200, response.json()
    assert isinstance(response.json()["logs"], list)  # List of logs is returned
    assert len(response.json()["logs"]) == 0  # Logs are empty


@pytest.mark.anyio
async def test_get_logs_w_pagination(client: AsyncClient):
    project_name = "eval-project"
    _ = await _create_project(client, project_name)
    _ = await _create_several_logs(client, project_name)

    # limit = 3
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"limit": 3},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 3

    assert result["logs"][0]["entries"] == {
        "_/_data": {"a": 8, "b": 10},
        "_/timestamp": (datetime(1993, 3, 24, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][1]["entries"] == {
        "_/description": "air",
        "_/metadata": [3, 8, 5],
        "_/_data": {"a": 6, "b": 12, "c": 8, "d": 11},
        "_/timestamp": (datetime(1993, 3, 24, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][2]["entries"] == {
        "_/description": "lava",
        "_/metadata": [1, 5, 6],
        "_/_data": {"a": 2, "b": 4},
        "_/timestamp": (datetime(1993, 3, 24, tzinfo=timezone.utc)).isoformat(),
    }

    # limit = 3 and offset = 2
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"limit": 3, "offset": 2},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 3

    assert result["logs"][0]["entries"] == {
        "_/description": "lava",
        "_/metadata": [1, 5, 6],
        "_/_data": {"a": 2, "b": 4},
        "_/timestamp": (datetime(1993, 3, 24, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][1]["entries"] == {
        "_/description": "freezing nitrogen",
        "_/temperature": -210.0,
        "_/state": "liquid->solid",
        "_/safe": False,
        "_/timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][2]["entries"] == {
        "_/description": "surface of the sun",
        "_/temperature": 6000.0,
        "_/state": "gas",
        "_/safe": False,
        "_/timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
    }

    # offset = 5
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"offset": 5},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 2

    assert result["logs"][0]["entries"] == {
        "_/description": "freezing water",
        "_/temperature": 0.0,
        "_/state": "liquid->solid",
        "_/safe": True,
        "_/timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][1]["entries"] == {
        "_/description": "boiling water",
        "_/temperature": 100.0,
        "_/state": "liquid->gas",
        "_/safe": False,
        "_/timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
    }


@pytest.mark.anyio
async def test_get_logs_w_filtering(client: AsyncClient):
    project_name = "eval-project"
    _ = await _create_project(client, project_name)
    _ = await _create_several_logs(client, project_name)

    # temperature == -210.0
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "_/temperature == -210.0"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 1
    assert isinstance(result["logs"][0]["ts"], str)
    assert result["logs"][0]["entries"] == {
        "_/description": "freezing nitrogen",
        "_/temperature": -210.0,
        "_/state": "liquid->solid",
        "_/safe": False,
        "_/timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
    }

    # temperature != -210.0
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "_/temperature != -210.0"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 3
    assert isinstance(result["logs"][0]["ts"], str)
    assert {
        "_/description": "freezing nitrogen",
        "_/temperature": -210.0,
        "_/state": "liquid->solid",
        "_/safe": False,
        "_/timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
    } not in [log["entries"] for log in result["logs"]]

    # temperature > 0.
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "_/temperature > 0."},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 2
    assert isinstance(result["logs"][0]["ts"], str)
    assert isinstance(result["logs"][1]["ts"], str)
    assert result["logs"][0]["entries"] == {
        "_/description": "surface of the sun",
        "_/temperature": 6000.0,
        "_/state": "gas",
        "_/safe": False,
        "_/timestamp": datetime(1993, 3, 22, tzinfo=timezone.utc).isoformat(),
    }
    assert result["logs"][1]["entries"] == {
        "_/description": "boiling water",
        "_/temperature": 100.0,
        "_/state": "liquid->gas",
        "_/safe": False,
        "_/timestamp": datetime(1993, 3, 22, tzinfo=timezone.utc).isoformat(),
    }

    # timestamp later than 23/03/1993
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": '_/timestamp > "1993-03-23"'},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 3
    assert result["logs"][0]["entries"] == {
        "_/_data": {"a": 8, "b": 10},
        "_/timestamp": (datetime(1993, 3, 24, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][1]["entries"] == {
        "_/description": "air",
        "_/metadata": [3, 8, 5],
        "_/_data": {"a": 6, "b": 12, "c": 8, "d": 11},
        "_/timestamp": (datetime(1993, 3, 24, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][2]["entries"] == {
        "_/description": "lava",
        "_/metadata": [1, 5, 6],
        "_/_data": {"a": 2, "b": 4},
        "_/timestamp": (datetime(1993, 3, 24, tzinfo=timezone.utc)).isoformat(),
    }

    # timestamp earlier than 23/03/1993
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": '_/timestamp < "1993-03-23"'},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 4
    assert result["logs"][0]["entries"] == {
        "_/description": "freezing nitrogen",
        "_/temperature": -210.0,
        "_/state": "liquid->solid",
        "_/safe": False,
        "_/timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][1]["entries"] == {
        "_/description": "surface of the sun",
        "_/temperature": 6000.0,
        "_/state": "gas",
        "_/safe": False,
        "_/timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][2]["entries"] == {
        "_/description": "freezing water",
        "_/temperature": 0.0,
        "_/state": "liquid->solid",
        "_/safe": True,
        "_/timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][3]["entries"] == {
        "_/description": "boiling water",
        "_/temperature": 100.0,
        "_/state": "liquid->gas",
        "_/safe": False,
        "_/timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
    }

    # timestamp is 23/03/1993
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": '_/timestamp == "1993-03-23"'},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 0

    # is earlier than or later than 23/03/1993
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={
            "filter_expr": '_/timestamp < "1993-03-23" or _/timestamp > "1993-03-23"',
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 7

    # safe is True
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "_/safe is True"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 1
    assert result["logs"][0]["entries"] == {
        "_/description": "freezing water",
        "_/temperature": 0.0,
        "_/state": "liquid->solid",
        "_/safe": True,
        "_/timestamp": datetime(1993, 3, 22, tzinfo=timezone.utc).isoformat(),
    }

    # liquid not in state
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "'liquid' not in _/state"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 1
    assert result["logs"][0]["entries"] == {
        "_/description": "surface of the sun",
        "_/temperature": 6000.0,
        "_/state": "gas",
        "_/safe": False,
        "_/timestamp": datetime(1993, 3, 22, tzinfo=timezone.utc).isoformat(),
    }

    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "_/description == 'boiling water'"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 1
    assert result["logs"][0]["entries"]["_/description"] == "boiling water"

    # check multiple conditions
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "('liquid' not in _/state) or (_/temperature == 0)"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 2
    assert result["logs"][0]["entries"] == {
        "_/description": "surface of the sun",
        "_/temperature": 6000.0,
        "_/state": "gas",
        "_/safe": False,
        "_/timestamp": datetime(1993, 3, 22, tzinfo=timezone.utc).isoformat(),
    }
    assert result["logs"][1]["entries"] == {
        "_/description": "freezing water",
        "_/temperature": 0.0,
        "_/state": "liquid->solid",
        "_/safe": True,
        "_/timestamp": datetime(1993, 3, 22, tzinfo=timezone.utc).isoformat(),
    }

    # Test filtering by updated_at and created_at timestamps
    # Update some logs to create a time difference
    log_ids = [1, 2]
    initial_time = datetime.now(timezone.utc)
    entries = {"_/state": "gas->liquid"}
    update_response = await client.put(
        f"/v0/logs",
        json={"ids": log_ids, "entries": entries, "overwrite": True},
        headers=HEADERS,
    )
    assert update_response.status_code == 200

    # Now test filtering for logs where updated_at > created_at
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "updated_at > created_at"},
        headers=HEADERS,
    )
    assert response.status_code == 200
    result = response.json()
    updated_logs = result["logs"]
    assert len(updated_logs) == 2  # Should find the two updated logs
    # # Verify timestamps were updated
    # for log in updated_logs:
    #     assert datetime.fromisoformat(log["updated_at"]) > datetime.fromisoformat(log["created_at"])
    log_ids_found = [log["id"] for log in result["logs"]]
    assert log_ids_found == [2, 1]

    # Test filtering for logs where updated_at = created_at
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "updated_at == created_at"},
        headers=HEADERS,
    )
    assert response.status_code == 200
    result = response.json()
    # Should find the non-updated logs where updated_at equals created_at
    assert len(result["logs"]) == 5  # Should find the non-updated logs
    log_ids_found = [log["id"] for log in result["logs"]]
    assert log_ids_found == [7, 6, 5, 4, 3]
    # Test combining timestamp filters with other fields
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={
            "filter_expr": "updated_at > created_at and _/state == 'gas->liquid'",
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    result = response.json()
    assert len(result["logs"]) == 2
    for log in result["logs"]:
        assert log["entries"]["_/state"] == "gas->liquid"

    # Test filtering by updated_at range
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={
            "filter_expr": f'updated_at >= "{initial_time.isoformat()}"',
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    result = response.json()
    assert len(result["logs"]) == 2  # Should only find the updated logs
    for log in result["logs"]:
        assert log["entries"]["_/state"] == "gas->liquid"

    # check exists
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "exists(_/state)"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 4

    # check not exists
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "not exists(_/temperature)"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 3

    # Test log_id equality filtering
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "log_id == 1"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 1
    assert result["logs"][0]["id"] == 1

    # Test log_id inequality filtering
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "log_id != 1"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) > 0
    assert all(log["id"] != 1 for log in result["logs"])

    # Test log_id in operator
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "log_id in [1, 2, 3]"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) > 0
    assert all(log["id"] in [1, 2, 3] for log in result["logs"])

    # Test log_id not in operator
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "log_id not in [1, 2, 3]"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) > 0
    assert all(log["id"] not in [1, 2, 3] for log in result["logs"])

    # Test nested conditions with log_id
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "log_id > 2 and _/temperature > 0"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) > 0
    for log in result["logs"]:
        assert log["id"] > 2
        assert log["entries"]["_/temperature"] > 0

    # Test non-existent log_id
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "log_id == 9999"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 0

    # Test log_id with complex nested conditions
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={
            "filter_expr": "(log_id > 1 and log_id < 4) and (_/temperature > 0 or _/safe is True)",
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) > 0
    for log in result["logs"]:
        assert 1 < log["id"] < 4
        assert (
            log["entries"].get("_/temperature", 0) > 0
            or log["entries"].get("_/safe") is True
        )

    # check len
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "len(_/description) < 10"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 2
    assert result["logs"][1]["entries"]["_/description"] == "lava"

    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "len(_/_data) > 2"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 1
    assert result["logs"][0]["entries"]["_/description"] == "air"

    # check in
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "'lava' in _/description"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 1
    assert result["logs"][0]["entries"]["_/description"] == "lava"

    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "version('a/b/param1') == 1"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 1
    assert result["logs"][0]["params"]["a/b/param1"] == "1"


@pytest.mark.anyio
async def test_get_logs_w_str_filtering(client: AsyncClient):
    project_name = "eval-project"
    _ = await _create_project(client, project_name)
    _ = await _create_several_logs(client, project_name)

    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "'2' in to_str(_/_data)"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 2

    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": """'{"a": 2' in to_str(_/_data)"""},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 1


@pytest.mark.anyio
async def test_get_logs_w_sorting(client: AsyncClient):
    project_name = "eval-project"
    _ = await _create_project(client, project_name)
    _ = await _create_several_logs(client, project_name)

    # descending creation time (default)
    response = await client.get(
        f"/v0/logs?project={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 7
    assert result["logs"][0]["entries"] == {
        "_/_data": {"a": 8, "b": 10},
        "_/timestamp": (datetime(1993, 3, 24, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][1]["entries"] == {
        "_/description": "air",
        "_/metadata": [3, 8, 5],
        "_/_data": {"a": 6, "b": 12, "c": 8, "d": 11},
        "_/timestamp": (datetime(1993, 3, 24, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][2]["entries"] == {
        "_/description": "lava",
        "_/metadata": [1, 5, 6],
        "_/_data": {"a": 2, "b": 4},
        "_/timestamp": (datetime(1993, 3, 24, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][3]["entries"] == {
        "_/description": "freezing nitrogen",
        "_/temperature": -210.0,
        "_/state": "liquid->solid",
        "_/safe": False,
        "_/timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][4]["entries"] == {
        "_/description": "surface of the sun",
        "_/temperature": 6000.0,
        "_/state": "gas",
        "_/safe": False,
        "_/timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][5]["entries"] == {
        "_/description": "freezing water",
        "_/temperature": 0.0,
        "_/state": "liquid->solid",
        "_/safe": True,
        "_/timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][6]["entries"] == {
        "_/description": "boiling water",
        "_/temperature": 100.0,
        "_/state": "liquid->gas",
        "_/safe": False,
        "_/timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
    }

    # ascending temperature
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"sorting": json.dumps({"_/temperature": "ascending"})},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 7
    assert result["logs"][0]["entries"] == {
        "_/description": "freezing nitrogen",
        "_/temperature": -210.0,
        "_/state": "liquid->solid",
        "_/safe": False,
        "_/timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][1]["entries"] == {
        "_/description": "freezing water",
        "_/temperature": 0.0,
        "_/state": "liquid->solid",
        "_/safe": True,
        "_/timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][2]["entries"] == {
        "_/description": "boiling water",
        "_/temperature": 100.0,
        "_/state": "liquid->gas",
        "_/safe": False,
        "_/timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][3]["entries"] == {
        "_/description": "surface of the sun",
        "_/temperature": 6000.0,
        "_/state": "gas",
        "_/safe": False,
        "_/timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][4]["entries"] == {
        "_/_data": {"a": 8, "b": 10},
        "_/timestamp": (datetime(1993, 3, 24, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][5]["entries"] == {
        "_/description": "air",
        "_/metadata": [3, 8, 5],
        "_/_data": {"a": 6, "b": 12, "c": 8, "d": 11},
        "_/timestamp": (datetime(1993, 3, 24, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][6]["entries"] == {
        "_/description": "lava",
        "_/metadata": [1, 5, 6],
        "_/_data": {"a": 2, "b": 4},
        "_/timestamp": (datetime(1993, 3, 24, tzinfo=timezone.utc)).isoformat(),
    }

    # descending safety, then ascending temperature
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={
            "sorting": json.dumps(
                {
                    "_/safe": "descending",
                    "_/temperature": "ascending",
                },
            ),
        },
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 7
    assert result["logs"][0]["entries"] == {
        "_/description": "freezing water",
        "_/temperature": 0.0,
        "_/state": "liquid->solid",
        "_/safe": True,
        "_/timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][1]["entries"] == {
        "_/description": "freezing nitrogen",
        "_/temperature": -210.0,
        "_/state": "liquid->solid",
        "_/safe": False,
        "_/timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][2]["entries"] == {
        "_/description": "boiling water",
        "_/temperature": 100.0,
        "_/state": "liquid->gas",
        "_/safe": False,
        "_/timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][3]["entries"] == {
        "_/description": "surface of the sun",
        "_/temperature": 6000.0,
        "_/state": "gas",
        "_/safe": False,
        "_/timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][4]["entries"] == {
        "_/_data": {"a": 8, "b": 10},
        "_/timestamp": (datetime(1993, 3, 24, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][5]["entries"] == {
        "_/description": "air",
        "_/metadata": [3, 8, 5],
        "_/_data": {"a": 6, "b": 12, "c": 8, "d": 11},
        "_/timestamp": (datetime(1993, 3, 24, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][6]["entries"] == {
        "_/description": "lava",
        "_/metadata": [1, 5, 6],
        "_/_data": {"a": 2, "b": 4},
        "_/timestamp": (datetime(1993, 3, 24, tzinfo=timezone.utc)).isoformat(),
    }

    # ascending _data
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"sorting": json.dumps({"_/_data": "ascending"})},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 7
    assert result["logs"][0]["entries"] == {
        "_/description": "lava",
        "_/metadata": [1, 5, 6],
        "_/_data": {"a": 2, "b": 4},
        "_/timestamp": (datetime(1993, 3, 24, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][1]["entries"] == {
        "_/_data": {"a": 8, "b": 10},
        "_/timestamp": (datetime(1993, 3, 24, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][2]["entries"] == {
        "_/description": "air",
        "_/metadata": [3, 8, 5],
        "_/_data": {"a": 6, "b": 12, "c": 8, "d": 11},
        "_/timestamp": (datetime(1993, 3, 24, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][3]["entries"] == {
        "_/description": "freezing nitrogen",
        "_/temperature": -210.0,
        "_/state": "liquid->solid",
        "_/safe": False,
        "_/timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][4]["entries"] == {
        "_/description": "surface of the sun",
        "_/temperature": 6000.0,
        "_/state": "gas",
        "_/safe": False,
        "_/timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][5]["entries"] == {
        "_/description": "freezing water",
        "_/temperature": 0.0,
        "_/state": "liquid->solid",
        "_/safe": True,
        "_/timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][6]["entries"] == {
        "_/description": "boiling water",
        "_/temperature": 100.0,
        "_/state": "liquid->gas",
        "_/safe": False,
        "_/timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
    }

    # descending metadata
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"sorting": json.dumps({"_/metadata": "descending"})},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 7
    assert result["logs"][0]["entries"] == {
        "_/description": "air",
        "_/metadata": [3, 8, 5],
        "_/_data": {"a": 6, "b": 12, "c": 8, "d": 11},
        "_/timestamp": (datetime(1993, 3, 24, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][1]["entries"] == {
        "_/description": "lava",
        "_/metadata": [1, 5, 6],
        "_/_data": {"a": 2, "b": 4},
        "_/timestamp": (datetime(1993, 3, 24, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][2]["entries"] == {
        "_/_data": {"a": 8, "b": 10},
        "_/timestamp": (datetime(1993, 3, 24, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][3]["entries"] == {
        "_/description": "freezing nitrogen",
        "_/temperature": -210.0,
        "_/state": "liquid->solid",
        "_/safe": False,
        "_/timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][4]["entries"] == {
        "_/description": "surface of the sun",
        "_/temperature": 6000.0,
        "_/state": "gas",
        "_/safe": False,
        "_/timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][5]["entries"] == {
        "_/description": "freezing water",
        "_/temperature": 0.0,
        "_/state": "liquid->solid",
        "_/safe": True,
        "_/timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][6]["entries"] == {
        "_/description": "boiling water",
        "_/temperature": 100.0,
        "_/state": "liquid->gas",
        "_/safe": False,
        "_/timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
    }


@pytest.mark.anyio
async def test_get_logs_w_timestamp_sorting(client: AsyncClient):
    project_name = "eval-project"
    _ = await _create_project(client, project_name)
    data = log_data["logs_for_various"]
    timestamps = list()
    for i in range(len(data)):
        ts = datetime.now(timezone.utc).isoformat()
        timestamps.append(ts)
        entries = data[i]
        entries["_/timestamp"] = ts
        response = await client.post(
            "/v0/logs",
            json={
                "project": project_name,
                "params": {"a/b/param1": f"test_{i}"},
                "entries": entries,
            },
            headers=HEADERS,
        )
        assert response.status_code == 200, response.json()

    # descending timestamp
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"sorting": json.dumps({"_/timestamp": "descending"})},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 7
    assert result["logs"][0]["entries"] == {
        "_/_data": {"a": 8, "b": 10},
        "_/timestamp": timestamps[-1],
    }
    assert result["logs"][1]["entries"] == {
        "_/description": "air",
        "_/metadata": [3, 8, 5],
        "_/_data": {"a": 6, "b": 12, "c": 8, "d": 11},
        "_/timestamp": timestamps[-2],
    }
    assert result["logs"][2]["entries"] == {
        "_/description": "lava",
        "_/metadata": [1, 5, 6],
        "_/_data": {"a": 2, "b": 4},
        "_/timestamp": timestamps[-3],
    }
    assert result["logs"][3]["entries"] == {
        "_/description": "freezing nitrogen",
        "_/temperature": -210.0,
        "_/state": "liquid->solid",
        "_/safe": False,
        "_/timestamp": timestamps[-4],
    }
    assert result["logs"][4]["entries"] == {
        "_/description": "surface of the sun",
        "_/temperature": 6000.0,
        "_/state": "gas",
        "_/safe": False,
        "_/timestamp": timestamps[-5],
    }
    assert result["logs"][5]["entries"] == {
        "_/description": "freezing water",
        "_/temperature": 0.0,
        "_/state": "liquid->solid",
        "_/safe": True,
        "_/timestamp": timestamps[-6],
    }
    assert result["logs"][6]["entries"] == {
        "_/description": "boiling water",
        "_/temperature": 100.0,
        "_/state": "liquid->gas",
        "_/safe": False,
        "_/timestamp": timestamps[-7],
    }


@pytest.mark.anyio
async def test_get_logs_w_date_sorting(client: AsyncClient):
    project_name = "eval-project"
    _ = await _create_project(client, project_name)
    data = log_data["logs_for_various"]
    dates = list()
    for i in range(len(data)):
        date = datetime(1993, 3, i + 1, tzinfo=timezone.utc).strftime("%Y-%m-%d")
        dates.append(date)
        entries = data[i]
        entries["_/timestamp"] = date
        response = await client.post(
            "/v0/logs",
            json={
                "project": project_name,
                "params": {"a/b/param1": f"test_{i}"},
                "entries": entries,
            },
            headers=HEADERS,
        )
        assert response.status_code == 200, response.json()

    # descending timestamp
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"sorting": json.dumps({"_/timestamp": "descending"})},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 7
    assert result["logs"][0]["entries"] == {
        "_/_data": {"a": 8, "b": 10},
        "_/timestamp": dates[-1],
    }
    assert result["logs"][1]["entries"] == {
        "_/description": "air",
        "_/metadata": [3, 8, 5],
        "_/_data": {"a": 6, "b": 12, "c": 8, "d": 11},
        "_/timestamp": dates[-2],
    }
    assert result["logs"][2]["entries"] == {
        "_/description": "lava",
        "_/metadata": [1, 5, 6],
        "_/_data": {"a": 2, "b": 4},
        "_/timestamp": dates[-3],
    }
    assert result["logs"][3]["entries"] == {
        "_/description": "freezing nitrogen",
        "_/temperature": -210.0,
        "_/state": "liquid->solid",
        "_/safe": False,
        "_/timestamp": dates[-4],
    }
    assert result["logs"][4]["entries"] == {
        "_/description": "surface of the sun",
        "_/temperature": 6000.0,
        "_/state": "gas",
        "_/safe": False,
        "_/timestamp": dates[-5],
    }
    assert result["logs"][5]["entries"] == {
        "_/description": "freezing water",
        "_/temperature": 0.0,
        "_/state": "liquid->solid",
        "_/safe": True,
        "_/timestamp": dates[-6],
    }
    assert result["logs"][6]["entries"] == {
        "_/description": "boiling water",
        "_/temperature": 100.0,
        "_/state": "liquid->gas",
        "_/safe": False,
        "_/timestamp": dates[-7],
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
async def test_get_logs_nested_dict_ordering(client: AsyncClient):
    """Test that nested dictionary key ordering is preserved at multiple levels."""
    project_name = "nested-dict-order-test"
    _ = await _create_project(client, project_name)

    # Create a log with deeply nested dictionaries in specific orders
    nested_data = {
        "level1": {
            "c": {
                "inner3": 3,
                "inner2": 2,
                "inner1": 1,
                "nested": {
                    "z": "last",
                    "y": "middle",
                    "x": "first",
                },
            },
            "b": {
                "foo": "bar",
                "baz": "qux",
                "empty_dict": {},
                "list_of_dicts": [
                    {"d3": 3, "d2": 2, "d1": 1},
                    {"z": "z", "y": "y", "x": "x"},
                ],
            },
            "a": "value",
        },
        "edge_cases": {
            "empty": {},
            "mixed_types": {
                "num": 42,
                "str": "text",
                "bool": True,
                "null": None,
                "list": [1, 2, 3],
                "nested_empty": {"empty": {}},
            },
        },
        "simple": "field",
    }

    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "entries": nested_data,
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Retrieve and verify the log
    response = await client.get(
        f"/v0/logs?project={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200

    log = response.json()["logs"][0]["entries"]

    # Verify top level ordering
    assert list(log.keys()) == ["level1", "edge_cases", "simple"]

    # Verify level1 ordering
    level1 = log["level1"]
    assert list(level1.keys()) == ["c", "b", "a"]

    # Verify inner dictionary ordering
    inner_c = level1["c"]
    assert list(inner_c.keys()) == ["inner3", "inner2", "inner1", "nested"]
    assert list(inner_c["nested"].keys()) == ["z", "y", "x"]

    inner_b = level1["b"]
    assert list(inner_b.keys()) == ["foo", "baz", "empty_dict", "list_of_dicts"]
    assert inner_b["empty_dict"] == {}

    # Verify ordering in list of dictionaries
    list_of_dicts = inner_b["list_of_dicts"]
    assert len(list_of_dicts) == 2
    assert list(list_of_dicts[0].keys()) == ["d3", "d2", "d1"]
    assert list(list_of_dicts[1].keys()) == ["z", "y", "x"]

    # Verify edge cases
    edge_cases = log["edge_cases"]
    assert list(edge_cases.keys()) == ["empty", "mixed_types"]
    assert edge_cases["empty"] == {}

    mixed_types = edge_cases["mixed_types"]
    assert list(mixed_types.keys()) == [
        "num",
        "str",
        "bool",
        "null",
        "list",
        "nested_empty",
    ]
    assert mixed_types["nested_empty"] == {"empty": {}}


async def test_get_logs_project_not_found(client: AsyncClient):
    project_name = "non_existent_project"

    # This should return 404 as the project does not exist
    response = await client.get(f"/v0/logs?project={project_name}", headers=HEADERS)

    assert response.status_code == 404, response.json()
    assert response.json() == {
        "detail": f"Project {project_name} not found.",
    }


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
async def test_delete_logs(client: AsyncClient):
    project_name = "multi-log-project"
    _ = await _create_project(client, project_name)

    # Create multiple logs
    response1 = await _create_log(client, project_name)
    response2 = await _create_log(client, project_name)
    assert response1.status_code == 200, response1.json()
    assert response2.status_code == 200, response2.json()

    log_id1 = response1.json()[0]
    log_id2 = response2.json()[0]
    ids_and_fields = [([log_id1, log_id2], None)]

    # Delete the logs
    response = await _delete_logs(client, ids_and_fields)
    assert response.status_code == 200, response.json()
    assert response.json()["info"] == "Logs and fields deleted successfully!"

    # Verify logs were deleted
    response = await _get_log(client, project_name, log_id1)
    assert response.status_code == 200, response.json()
    assert response.json() == {"params": {}, "logs": [], "count": 0}

    response = await _get_log(client, project_name, log_id2)
    assert response.status_code == 200, response.json()
    assert response.json() == {"params": {}, "logs": [], "count": 0}


@pytest.mark.anyio
async def test_update_logs(client: AsyncClient):
    project_name = "multi-log-project"
    _ = await _create_project(client, project_name)

    # Create multiple logs
    response1 = await _create_log(client, project_name)
    response2 = await _create_log(client, project_name)
    assert response1.status_code == 200, response1.json()
    assert response2.status_code == 200, response2.json()

    log_id1 = response1.json()[0]
    log_id2 = response2.json()[0]
    log_ids = [log_id1, log_id2]

    # Update both logs
    entries = {
        "new_entry": "Updated value",
        "explicit_types": {"new_entry": {"type": "str", "mutable": True}},
    }
    response = await _update_logs(client, log_ids, entries)
    assert response.status_code == 200, response.json()
    assert response.json()["info"] == "Logs updated successfully!"

    # Verify updates
    response = await _get_log(client, project_name, log_id1)
    assert response.status_code == 200, response.json()
    assert response.json()["logs"][0]["entries"]["new_entry"] == "Updated value"

    response = await _get_log(client, project_name, log_id2)
    assert response.status_code == 200, response.json()
    assert response.json()["logs"][0]["entries"]["new_entry"] == "Updated value"


@pytest.mark.anyio
async def test_update_logs_multi_values(client: AsyncClient):
    project_name = "multi-log-project"
    _ = await _create_project(client, project_name)

    # Create multiple logs
    response1 = await _create_log(client, project_name)
    response2 = await _create_log(client, project_name)
    assert response1.status_code == 200, response1.json()
    assert response2.status_code == 200, response2.json()

    log_id1 = response1.json()[0]
    log_id2 = response2.json()[0]
    log_ids = [log_id1, log_id2]

    # Update both logs
    entries = [
        {
            "new_entry": "First updated value",
            "explicit_types": {"new_entry": {"type": "str", "mutable": True}},
        },
        {
            "new_entry": "Second updated value",
            "explicit_types": {"new_entry": {"type": "str", "mutable": True}},
        },
    ]
    response = await _update_logs(client, log_ids, entries)
    assert response.status_code == 200, response.json()
    assert response.json()["info"] == "Logs updated successfully!"

    # Verify updates
    response = await _get_log(client, project_name, log_id1)
    assert response.status_code == 200, response.json()
    assert response.json()["logs"][0]["entries"]["new_entry"] == "First updated value"

    response = await _get_log(client, project_name, log_id2)
    assert response.status_code == 200, response.json()
    assert response.json()["logs"][0]["entries"]["new_entry"] == "Second updated value"


@pytest.mark.anyio
async def test_delete_log_fields_from_logs(client: AsyncClient):
    project_name = "multi-log-project"
    _ = await _create_project(client, project_name)

    # Create multiple logs
    response1 = await _create_log(client, project_name)
    response2 = await _create_log(client, project_name)
    assert response1.status_code == 200, response1.json()
    assert response2.status_code == 200, response2.json()

    log_id1 = response1.json()[0]
    log_id2 = response2.json()[0]
    entry_to_delete = "a/b/c/input"
    ids_and_fields = [(log_id1, entry_to_delete), (log_id2, entry_to_delete)]

    # Delete entries from the logs
    response = await _delete_log_fields_from_logs(client, ids_and_fields)
    assert response.status_code == 200, response.json()
    assert response.json()["info"] == "Logs and fields deleted successfully!"

    # Verify deletion of entry
    response = await _get_log(client, project_name, log_id1)
    assert response.status_code == 200, response.json()
    assert entry_to_delete not in response.json()["logs"][0]["entries"]

    response = await _get_log(client, project_name, log_id2)
    assert response.status_code == 200, response.json()
    assert entry_to_delete not in response.json()["logs"][0]["entries"]

    ids_and_fields = [
        (log_id1, ["a/b/c/boolean_input", "a/b/c/numeric_input", "a/b/param1"]),
        ([log_id1, log_id2], ["a/b/c/boolean_input", "a/b/param1"]),
    ]
    # Delete entries from the logs
    response = await _delete_log_fields_from_logs(
        client,
        ids_and_fields,
        delete_empty_logs=True,
    )
    assert response.status_code == 200, response.json()
    assert response.json()["info"] == "Logs and fields deleted successfully!"

    response = await client.get(f"/v0/logs?project={project_name}", headers=HEADERS)
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 1
    del result["logs"][0]["ts"]
    assert result["logs"] == [
        {
            "id": 2,
            "entries": {"a/b/c/numeric_input": 4.5},
            "params": {},
            "derived_entries": {},
            "clipped_fields": [],
        },
    ]


@pytest.mark.anyio
async def test_create_log_strongly_typed(client: AsyncClient):
    project_name = "test_project"
    _ = await _create_project(client, project_name)

    # Create a log with strongly typed fields
    response = await _create_log(
        client,
        project_name,
        params={"a/b/param1": "test"},
        entries={"score": 10, "logged_at": datetime.now(timezone.utc).isoformat()},
    )

    assert response.status_code == 200, response.json()

    # Verify that field types are set correctly
    field_types_response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert field_types_response.status_code == 200
    assert field_types_response.json() == {
        "a/b/param1": {
            "data_type": "str",
            "field_type": "param",
            "mutable": True,
            "artifacts": "",
        },
        "score": {
            "data_type": "int",
            "field_type": "entry",
            "mutable": True,
            "artifacts": "",
        },
        "logged_at": {
            "data_type": "timestamp",
            "field_type": "entry",
            "mutable": True,
            "artifacts": "",
        },
    }


@pytest.mark.anyio
async def test_create_log_type_mismatch(client: AsyncClient):
    project_name = "test_project"
    _ = await _create_project(client, project_name)

    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "params": {"a/b/param1": "test"},
            "entries": {
                "score": 10,
                "response": "hello",
            },
        },
        headers=HEADERS,
    )
    # Create a log with a type mismatch
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "params": {"a/b/param1": True},  # This should cause a type mismatch
            "entries": {
                "score": "not_an_int",
                "response": "hello",
            },
        },
        headers=HEADERS,
    )

    assert response.status_code == 400
    assert "Type mismatch for field" in response.json()["detail"]


@pytest.mark.anyio
async def test_update_logs_strongly_typed(client: AsyncClient):
    project_name = "test_project"
    _ = await _create_project(client, project_name)

    # Create a log first
    response1 = await _create_log(client, project_name)
    log_id1 = response1.json()[0]

    # Update the log with strongly typed fields
    response = await client.put(
        f"/v0/logs",
        json={
            "ids": [log_id1],
            "entries": {
                "a/b/c/input": "new data",
                "a/b/c/numeric_input": -12.0,
            },
            "overwrite": True,
        },
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    assert response.json()["info"] == "Logs updated successfully!"


@pytest.mark.anyio
async def test_update_logs_previously_none(client: AsyncClient):
    project_name = "test_project"
    _ = await _create_project(client, project_name)

    # Create a log first
    response1 = await _create_log(
        client,
        project_name,
        params={"a/b/param1": "test"},
        entries={
            "a/b/c/input": "Some input data",
            "a/b/c/boolean_input": True,
            "a/b/c/numeric_input": None,
        },
    )
    log_id1 = response1.json()[0]

    # Verify numeric is NoneType
    field_types_response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert field_types_response.status_code == 200
    assert field_types_response.json()["a/b/c/numeric_input"] == {
        "data_type": "NoneType",
        "field_type": "entry",
        "mutable": True,
        "artifacts": "",
    }

    # Update the log with strongly typed fields, but previously None
    response = await client.put(
        f"/v0/logs",
        json={
            "ids": [log_id1],
            "entries": {
                "a/b/c/numeric_input": -12.0,
            },
            "overwrite": True,
        },
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    assert response.json()["info"] == "Logs updated successfully!"

    # Verify numeric is now float
    field_types_response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert field_types_response.status_code == 200
    assert field_types_response.json()["a/b/c/numeric_input"] == {
        "data_type": "float",
        "field_type": "entry",
        "mutable": True,
        "artifacts": "",
    }

    # Now update the field back to None and verify it works
    response = await client.put(
        f"/v0/logs",
        json={
            "ids": [log_id1],
            "entries": {
                "a/b/c/numeric_input": None,
            },
            "overwrite": True,
        },
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    assert response.json()["info"] == "Logs updated successfully!"

    # Verify numeric is still float type (since type was established)
    field_types_response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert field_types_response.status_code == 200
    assert field_types_response.json()["a/b/c/numeric_input"] == {
        "data_type": "float",
        "field_type": "entry",
        "mutable": True,
        "artifacts": "",
    }


@pytest.mark.anyio
async def test_update_field_mutability_only(client: AsyncClient):
    """Test updating field mutability without changing the value."""
    project_name = "test_mutability_update"
    _ = await _create_project(client, project_name)

    # Create initial log with a mutable field
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "entries": {
                "mutable_field": "test value",
                "explicit_types": {
                    "mutable_field": {"type": "str", "mutable": True},
                },
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    log_id = response.json()[0]

    # Verify field is initially mutable
    field_types_response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert field_types_response.status_code == 200
    field_types = field_types_response.json()
    assert field_types["mutable_field"]["mutable"] is True

    # Update only the mutability without changing the value
    response = await client.put(
        "/v0/logs",
        json={
            "ids": [log_id],
            "entries": {
                "explicit_types": {
                    "mutable_field": {"mutable": False},
                },
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Verify field is now immutable but value unchanged
    field_types_response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert field_types_response.status_code == 200
    field_types = field_types_response.json()
    assert field_types["mutable_field"]["mutable"] is False

    # Verify the value was not changed
    response = await client.get(
        f"/v0/logs?project={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    logs = response.json()["logs"]
    assert len(logs) == 1
    assert logs[0]["entries"]["mutable_field"] == "test value"

    # Attempt to modify the now-immutable field (should fail)
    response = await client.put(
        "/v0/logs",
        json={
            "ids": [log_id],
            "entries": {
                "mutable_field": "new value",
            },
            "overwrite": True,
        },
        headers=HEADERS,
    )
    assert response.status_code == 400
    assert (
        "Field 'mutable_field' in log id 1 is immutable and cannot be modified."
        in response.json()["detail"]
    )


@pytest.mark.anyio
async def test_update_logs_type_mismatch(client: AsyncClient):
    project_name = "test_project"
    _ = await _create_project(client, project_name)

    # Create a log first
    response1 = await _create_log(client, project_name)
    log_id1 = response1.json()[0]

    # Update the log with a type mismatch
    response = await client.put(
        f"/v0/logs",
        json={
            "ids": [log_id1],
            "entries": {
                "a/b/c/numeric_input": "not_an_int",  # This should cause a type mismatch
            },
            "overwrite": True,
        },
        headers=HEADERS,
    )

    assert response.status_code == 400
    assert "Type mismatch for field" in response.json()["detail"]


@pytest.mark.anyio
async def test_create_log_with_mutable_fields(client: AsyncClient):
    project_name = "test_mutable_fields"
    _ = await _create_project(client, project_name)

    # Create a log with both mutable and immutable fields using explicit_types
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "entries": {
                "mutable_field": "initial value",
                "immutable_field": "fixed value",
                "explicit_types": {
                    "mutable_field": {"type": "str", "mutable": True},
                    "immutable_field": {"type": "str", "mutable": False},
                },
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    log_id = response.json()[0]

    # Verify field types include mutability information
    field_types_response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert field_types_response.status_code == 200
    field_types = field_types_response.json()

    assert field_types["mutable_field"]["mutable"] is True
    assert field_types["immutable_field"]["mutable"] is False


@pytest.mark.anyio
async def test_update_mutable_and_immutable_fields(client: AsyncClient):
    project_name = "test_mutable_updates"
    _ = await _create_project(client, project_name)

    # Create initial log with mutable and immutable fields using explicit_types
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "entries": {
                "mutable_field": "initial value",
                "immutable_field": "fixed value",
                "explicit_types": {
                    "mutable_field": {"type": "str", "mutable": True},
                    "immutable_field": {"type": "str", "mutable": False},
                },
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    log_id = response.json()[0]

    # Test updating mutable field (should succeed)
    response = await client.put(
        "/v0/logs",
        json={
            "ids": [log_id],
            "entries": {
                "mutable_field": "updated value",
            },
            "overwrite": True,
        },
        headers=HEADERS,
    )
    assert response.status_code == 200

    # Verify mutable field was updated
    response = await client.get(
        f"/v0/logs?project={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    log_data = response.json()["logs"][0]
    assert log_data["entries"]["mutable_field"] == "updated value"
    assert log_data["entries"]["immutable_field"] == "fixed value"

    # Test updating immutable field (should fail)
    response = await client.put(
        "/v0/logs",
        json={
            "ids": [log_id],
            "entries": {
                "immutable_field": "attempted update",
            },
            "overwrite": True,
        },
        headers=HEADERS,
    )
    assert response.status_code == 400
    assert (
        "Field 'immutable_field' in log id 1 is immutable and cannot be modified."
        in response.json()["detail"]
    )


@pytest.mark.anyio
async def test_create_log_default_immutable(client: AsyncClient):
    project_name = "test_default_immutable"
    _ = await _create_project(client, project_name)

    # Create a log without specifying mutability (should default to immutable)
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "entries": {
                "default_field": "initial value",
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    log_id = response.json()[0]

    # Verify field is immutable by default
    field_types_response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert field_types_response.status_code == 200
    field_types = field_types_response.json()
    assert field_types["default_field"]["mutable"] is False

    # Attempt to update the default immutable field (should fail)
    response = await client.put(
        "/v0/logs",
        json={
            "ids": [log_id],
            "entries": {
                "default_field": "attempted update",
            },
            "overwrite": True,
        },
        headers=HEADERS,
    )
    assert response.status_code == 400
    assert (
        "Field 'default_field' in log id 1 is immutable and cannot be modified."
        in response.json()["detail"]
    )


@pytest.mark.anyio
async def test_get_fields_with_derived_entries(client: AsyncClient):
    project_name = "test_project_derived"
    _ = await _create_project(client, project_name)

    # Create base logs
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "params": {"param1": "test"},
            "entries": {
                "base_field": 100,
                "temperature": 25.5,
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    log_id = response.json()[0]

    # Create derived entries
    derived_configs = [
        {
            "key": "temp_plus_10",
            "equation": "{t:temperature} + 10",
            "referenced_logs": {"t": [log_id]},
        },
        {
            "key": "double_base",
            "equation": "{b:base_field} * 2",
            "referenced_logs": {"b": [log_id]},
        },
    ]

    for config in derived_configs:
        response = await _create_derived_entry(
            client,
            project_name,
            config["key"],
            config["equation"],
            config["referenced_logs"],
        )
        assert response.status_code == 200

    # Get field types and verify response
    response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200
    fields = response.json()

    # Verify base entries
    assert fields["base_field"]["field_type"] == "entry"
    assert fields["base_field"]["data_type"] == "int"
    assert fields["base_field"]["artifacts"] == ""

    assert fields["temperature"]["field_type"] == "entry"
    assert fields["temperature"]["data_type"] == "float"
    assert fields["temperature"]["artifacts"] == ""

    # Verify params
    assert fields["param1"]["field_type"] == "param"
    assert fields["param1"]["data_type"] == "str"
    assert fields["param1"]["artifacts"] == ""

    # Verify derived entries
    assert fields["temp_plus_10"]["field_type"] == "derived_entry"
    assert fields["temp_plus_10"]["data_type"] == "int"
    assert fields["temp_plus_10"]["artifacts"] == "{t:temperature} + 10"

    assert fields["double_base"]["field_type"] == "derived_entry"
    assert fields["double_base"]["data_type"] == "int"
    assert fields["double_base"]["artifacts"] == "{b:base_field} * 2"


# TODO: remove this test once we enforce strong typing on all fields.
@pytest.mark.anyio
async def test_get_set_field_typing(client: AsyncClient):
    project_name = "test_project"
    _ = await _create_project(client, project_name)

    await _create_log(client, project_name)

    field_types_response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert field_types_response.status_code == 200

    field_types = field_types_response.json()

    # ordering
    assert list(field_types.keys()) == [
        "a/b/param1",
        "a/b/c/input",
        "a/b/c/boolean_input",
        "a/b/c/numeric_input",
    ]

    # values
    assert field_types["a/b/c/input"]["data_type"] == "str"
    assert field_types["a/b/c/input"]["field_type"] == "entry"
    assert field_types["a/b/c/boolean_input"]["data_type"] == "bool"
    assert field_types["a/b/c/boolean_input"]["field_type"] == "entry"
    assert field_types["a/b/c/numeric_input"]["data_type"] == "float"
    assert field_types["a/b/c/numeric_input"]["field_type"] == "entry"
    assert field_types["a/b/param1"]["data_type"] == "str"
    assert field_types["a/b/param1"]["field_type"] == "param"

    # Set field typing for the log entries
    response = await client.post(
        f"/v0/logs/fields/types",
        params={"project": project_name},
        json={
            "types": {
                "a/b/c/input": True,
                "a/b/c/boolean_input": True,
                "a/b/c/numeric_input": False,  # delete typing for numeric_input
            },
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert response.json()["info"] == "Field types updated successfully!"

    field_types_response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert field_types_response.status_code == 200

    field_types = field_types_response.json()

    # ordering
    # numeric_input is not included in the response because it was deleted
    assert list(field_types.keys()) == [
        "a/b/param1",
        "a/b/c/input",
        "a/b/c/boolean_input",
    ]

    # values
    assert field_types["a/b/c/input"]["data_type"] == "str"
    assert field_types["a/b/c/input"]["field_type"] == "entry"
    assert field_types["a/b/c/boolean_input"]["data_type"] == "bool"
    assert field_types["a/b/c/boolean_input"]["field_type"] == "entry"
    assert field_types["a/b/param1"]["data_type"] == "str"
    assert field_types["a/b/param1"]["field_type"] == "param"


@pytest.mark.anyio
async def test_set_field_typing_non_homogeneous(client: AsyncClient):
    project_name = "test_project"
    _ = await _create_project(client, project_name)

    # create a log entry (with strongly_typed=True)
    await _create_log(client, project_name)

    # set strongly_typed as False for the field 'a/b/c/numeric_input'
    response = await client.post(
        f"/v0/logs/fields/types",
        params={"project": project_name},
        json={"types": {"a/b/c/numeric_input": False}},
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert response.json()["info"] == "Field types updated successfully!"

    # now add a non-homogenous entry
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "entries": {
                "a/b/c/numeric_input": True,
            },
        },
        headers=HEADERS,
    )

    # setting strongly_typed as True for 'a/b/c/numeric_input' should fail!
    response = await client.post(
        f"/v0/logs/fields/types",
        params={"project": project_name},
        json={"types": {"a/b/c/numeric_input": True}},
        headers=HEADERS,
    )
    assert response.status_code == 400
    assert (
        "Cannot enable typing for field 'a/b/c/numeric_input' as existing logs have different types."
        in response.json()["detail"]
    )


@pytest.mark.anyio
async def test_get_logs_grouping_all_scenarios(client: AsyncClient):
    # Test for the following:
    # - Single-level grouping (entries & params)
    # - Multi-level grouping
    # - group_offset / group_limit
    # - group_depth

    project_name = "test-grouping-comprehensive"
    _ = await _create_project(client, project_name)

    # 1) Create initial logs using your existing fixture
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


@pytest.mark.anyio
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

    # TODO(yusha): test group_by + sorting at the group level once group sorting is implemented

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
