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


def _create_log(client, project_name, user=1, entries=None):
    _headers = HEADERS if user == 1 else HEADERS_2
    if entries is None:
        entries = log_data["log"]
    return client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "params": {"a/b/param1": "test"},
            "entries": entries,
        },
        headers=_headers,
    )


def _create_derived_entry(client, project_name, key, equation, referenced_logs, user=1):
    _headers = HEADERS if user == 1 else HEADERS_2
    return client.put(
        "/v0/log/derived",
        json={
            "project": project_name,
            "key": key,
            "equation": equation,
            "referenced_logs": referenced_logs,
        },
        headers=_headers,
    )


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
        response = await client.post(
            "/v0/logs",
            json={"project": project_name, "entries": data[i]},
            headers=_headers,
        )
        assert response.status_code == 200, response.json()


async def _create_several_logs(client, project_name, user=1):
    _headers = HEADERS if user == 1 else HEADERS_2
    data = log_data["logs_for_various"]
    for i in range(len(data)):
        response = await client.post(
            "/v0/logs",
            json={
                "project": project_name,
                "params": {"a/b/param1": f"test_{i}"},
                "entries": data[i],
            },
            headers=_headers,
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
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "entries": {
                "img_raw": img,
                "img_url": "https://upload.wikimedia.org/wikipedia/commons/4/45/Eopsaltria_australis_-_Mogo_Campground.jpg",
            },
        },
        headers=HEADERS,
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
        "img_raw": {"data_type": "image", "field_type": "entry"},
        "img_url": {"data_type": "image", "field_type": "entry"},
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

    response = await client.post(
        "/v0/logs",
        json={"project": project_name, "entries": log_data["log"]},
        headers=HEADERS,
    )
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
        "explicit_types": {"new_entry": "string"},
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
    params = (
        {"key": key}
        if from_ids is None
        else {"key": key, "from_ids": "&".join([str(i) for i in from_ids])}
    )
    response = await client.get(
        f"/v0/logs/metric/{metric}?project={project_name}",
        params=params,
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
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

    # fetch log groups for a given key
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
        "explicit_types": {"new_entry": "string"},
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
            "explicit_types": {"new_entry": "string"},
        },
        {
            "new_entry": "Second updated value",
            "explicit_types": {"new_entry": "string"},
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
        },
    ]


@pytest.mark.anyio
async def test_create_log_strongly_typed(client: AsyncClient):
    project_name = "test_project"
    _ = await _create_project(client, project_name)

    # Create a log with strongly typed fields
    response = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "params": {"a/b/param1": "test"},
            "entries": {
                "score": 10,
                "logged_at": datetime.now(timezone.utc).isoformat(),
            },
        },
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()

    # Verify that field types are set correctly
    field_types_response = await client.get(
        f"/v0/logs/fields?project={project_name}",
        headers=HEADERS,
    )
    assert field_types_response.status_code == 200
    assert field_types_response.json() == {
        "a/b/param1": {"data_type": "str", "field_type": "param"},
        "score": {"data_type": "int", "field_type": "entry"},
        "logged_at": {"data_type": "timestamp", "field_type": "entry"},
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
    response1 = await client.post(
        "/v0/logs",
        json={
            "project": project_name,
            "params": {"a/b/param1": "test"},
            "entries": {
                "a/b/c/input": "Some input data",
                "a/b/c/boolean_input": True,
                "a/b/c/numeric_input": None,
            },
        },
        headers=HEADERS,
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
    }


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
async def test_get_logs_with_type_check(client: AsyncClient):
    project_name = "test_project"
    _ = await _create_project(client, project_name)

    # Create log entries with different types
    _ = await _create_several_logs(client, project_name)

    # Test filtering for float type
    response = await client.get(
        f"/v0/logs?project={project_name}&filter_expr=type(_/temperature) is float",
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert len(response.json()["logs"]) == 4

    # Test filtering for str type
    response = await client.get(
        f"/v0/logs?project={project_name}&filter_expr=type(_/state) is str",
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert len(response.json()["logs"]) == 4

    # Test filtering for a bool type
    response = await client.get(
        f"/v0/logs?project={project_name}&filter_expr=type(_/safe) is bool",
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert len(response.json()["logs"]) == 4

    # Test filtering for a timestamp type
    response = await client.get(
        f"/v0/logs?project={project_name}&filter_expr=type(_/timestamp) is timestamp",
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert len(response.json()["logs"]) == 7

    # Test filtering for a non-existent type
    response = await client.get(
        f"/v0/logs?project={project_name}&filter_expr=type(_/timestamp) is str",
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert len(response.json()["logs"]) == 0

    # Test filtering using `is not`
    response = await client.get(
        f"/v0/logs?project={project_name}&filter_expr=type(_/timestamp) is not str",
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert len(response.json()["logs"]) == 7
