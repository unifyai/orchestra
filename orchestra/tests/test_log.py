import json
import os
from datetime import datetime, timezone
from typing import List, Optional

import pytest
from httpx import AsyncClient, Request

from ..web.api.log.helpers import reduction_methods

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
    "logs_for_filtering_n_metrics": [
        {
            "description": "boiling water",
            "temperature": 100.0,
            "state": "liquid->gas",
            "safe": False,
            "timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
        },
        {
            "description": "freezing water",
            "temperature": 0.0,
            "state": "liquid->solid",
            "safe": True,
            "timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
        },
        {
            "description": "surface of the sun",
            "temperature": 6000.0,
            "state": "gas",
            "safe": False,
            "timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
        },
        {
            "description": "freezing nitrogen",
            "temperature": -210.0,
            "state": "liquid->solid",
            "safe": False,
            "timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
        },
        {
            "description": "lava",
            "metadata": [1, 5, 6],
            "_data": {"a": 2, "b": 4},
            "timestamp": (datetime(1993, 3, 24, tzinfo=timezone.utc)).isoformat(),
        },
        {
            "description": "air",
            "metadata": [3, 8, 5],
            "_data": {"a": 6, "b": 12, "c": 8, "d": 11},
            "timestamp": (datetime(1993, 3, 24, tzinfo=timezone.utc)).isoformat(),
        },
        {
            "_data": {"a": 8, "b": 10},
            "timestamp": (datetime(1993, 3, 24, tzinfo=timezone.utc)).isoformat(),
        },
    ],
}


def _create_log(client, project_name, user=1):
    _headers = HEADERS if user == 1 else HEADERS_2
    return client.post(
        "/v0/log",
        json={
            "project": project_name,
            "params": {"a/b/param1": "test"},
            "entries": log_data["log"],
        },
        headers=_headers,
    )


def _get_log(client, log_id, user=1):
    _headers = HEADERS if user == 1 else HEADERS_2
    return client.get(f"/v0/log/{log_id}", headers=_headers)


def _update_log(client, log_id, user=1):
    _headers = HEADERS if user == 1 else HEADERS_2
    return client.put(
        f"/v0/log/{log_id}",
        json={"entries": log_data["log_update"]},
        headers=_headers,
    )


def _update_log_w_overwrite(client, log_id, overwrite, user=1):
    _headers = HEADERS if user == 1 else HEADERS_2
    return client.put(
        f"/v0/log/{log_id}",
        json={"entries": log_data["log_update_w_overwrite"], "overwrite": overwrite},
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
        json={"ids": log_ids},
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


def _delete_log_field_from_logs(client, field, log_ids, user=1):
    _headers = HEADERS if user == 1 else HEADERS_2
    request = Request(
        "DELETE",
        str(client.base_url) + f"/v0/logs/field/{field}",
        json={"ids": log_ids},
        headers=_headers,
    )
    return client.send(request)


async def _create_logs_for_grouping(client, project_name, user=1):
    _headers = HEADERS if user == 1 else HEADERS_2
    data = log_data["logs_for_grouping"]
    for i in range(len(data)):
        response = await client.post(
            "/v0/log",
            json={"project": project_name, "entries": data[i]},
            headers=_headers,
        )
        assert response.status_code == 200, response.json()


async def _create_logs_for_filtering_n_metrics(client, project_name, user=1):
    _headers = HEADERS if user == 1 else HEADERS_2
    data = log_data["logs_for_filtering_n_metrics"]
    for i in range(len(data)):
        response = await client.post(
            "/v0/log",
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

    response = await _create_log(client, project_name)

    assert response.status_code == 200, response.json()
    assert isinstance(response.json(), int)

    # TODO: Get log and see if it matches


@pytest.mark.anyio
async def test_create_logs_autoincrement_version(client: AsyncClient):
    project_name = "non-matching-versions"
    _ = await _create_project(client, project_name)

    # This should work fine
    response = await client.post(
        "/v0/log",
        json={"project": project_name, "params": {"p1": "test"}},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # same version and value
    response = await client.post(
        "/v0/log",
        json={"project": project_name, "params": {"p1": "test"}},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # same version and different value -> autoincrement
    response = await client.post(
        "/v0/log",
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
        "/v0/log",
        json={"project": project_name, "entries": log_data["log"]},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    log_id = response.json()

    response = await _get_log(client, log_id)
    assert response.status_code == 200, response.json()
    orig_entries = response.json()["logs"]["entries"]
    assert len(orig_entries) == 3

    response = await client.post(
        "/v0/log",
        json={"project": project_name, "entries": log_data["log_update"]},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    log_id_2 = response.json()

    log_ids = [log_id, log_id_2]

    response = await _update_multiple_logs_w_overwrite(client, log_ids, overwrite=False)
    assert response.status_code == 400, response.json()

    response = await _update_multiple_logs_w_overwrite(client, log_ids, overwrite=True)
    assert response.status_code == 200, response.json()

    response = await _get_log(client, log_id)
    assert response.status_code == 200, response.json()
    new_entries = response.json()["logs"]["entries"]
    assert len(new_entries) == 3
    assert new_entries["a/b/c/input"] == orig_entries["a/b/c/input"]
    assert new_entries["a/b/c/boolean_input"] != orig_entries["a/b/c/boolean_input"]
    assert new_entries["a/b/c/numeric_input"] != orig_entries["a/b/c/numeric_input"]

    response = await _get_log(client, log_id_2)
    assert response.status_code == 200, response.json()
    new_entries = response.json()["logs"]["entries"]
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
    log_id = response.json()
    assert isinstance(log_id, int)

    # verify it exists
    response = await _get_log(client, log_id)
    assert response.status_code == 200, response.json()

    # Now delete the project
    response = await client.delete(url, headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["info"] == "Project deleted successfully"

    # Verify the log has gone
    response = await _get_log(client, log_id)
    assert response.status_code == 404, response.json()
    assert response.json() == {
        "detail": f"Log with id {log_id} not found.",
    }


@pytest.mark.anyio
async def test_get_log(client: AsyncClient):
    project_name = "eval-project"
    _ = await _create_project(client, project_name)
    log_response = await _create_log(client, project_name)
    log_id = log_response.json()

    # fetch the log
    response = await client.get(f"/v0/log/{log_id}", headers=HEADERS)

    assert response.status_code == 200, response.json()
    assert "logs" in response.json()
    assert "params" in response.json()
    assert isinstance(response.json()["params"]["a/b/param1"]["0"], str)
    assert isinstance(response.json()["logs"]["ts"], str)
    assert isinstance(response.json()["logs"]["params"]["a/b/param1"], str)
    assert isinstance(response.json()["logs"]["entries"]["a/b/c/boolean_input"], bool)
    assert isinstance(response.json()["logs"]["entries"]["a/b/c/numeric_input"], float)


@pytest.mark.anyio
async def test_get_log_not_found(client: AsyncClient):
    log_id = "123"

    # This should return 404 as the log does not exist
    response = await client.get(f"/v0/log/{log_id}", headers=HEADERS)

    assert response.status_code == 404, response.json()
    assert response.json() == {
        "detail": f"Log with id {log_id} not found.",
    }


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
        "/v0/log",
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

    # fetch entries for the empty project
    response = await client.get(f"/v0/logs?project={project_name}", headers=HEADERS_2)

    assert response.status_code == 200, response.json()
    assert isinstance(response.json()["logs"], list)
    assert len(response.json()["logs"]) == 0


@pytest.mark.anyio
async def test_get_logs_latest_timestamp(client: AsyncClient):

    # create logs
    project_name = "eval-project"
    _ = await _create_project(client, project_name, user=1)
    t0 = datetime.now(timezone.utc)
    _ = await _create_logs_for_filtering_n_metrics(client, project_name, user=1)

    # assert the latest timestamp t1 is more recent than t0
    response = await client.get(
        f"/v0/logs/latest_timestamp?project={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    response = response.json()
    assert isinstance(response, str)
    t1 = datetime.fromisoformat(response).astimezone(timezone.utc)
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
    t2 = datetime.fromisoformat(response).astimezone(timezone.utc)
    assert t2 > t1


@pytest.mark.anyio
async def test_get_log_ids(client: AsyncClient):
    project_name = "eval-project"
    # create the same project with another user to ensure the correct one
    # is fetched
    _ = await _create_project(client, project_name, user=2)
    _ = await _create_project(client, project_name, user=1)
    _ = await _create_logs_for_filtering_n_metrics(client, project_name, user=1)

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
    assert response == list(range(1, 8))


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
async def test_get_logs_w_filtering(client: AsyncClient):
    project_name = "eval-project"
    _ = await _create_project(client, project_name)
    _ = await _create_logs_for_filtering_n_metrics(client, project_name)

    # temperature > 0.
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "temperature > 0."},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 2
    assert isinstance(result["logs"][0]["ts"], str)
    assert isinstance(result["logs"][1]["ts"], str)
    assert result["logs"][0]["entries"] == {
        "description": "boiling water",
        "temperature": 100.0,
        "state": "liquid->gas",
        "safe": False,
        "timestamp": datetime(1993, 3, 22, tzinfo=timezone.utc).isoformat(),
    }
    assert result["logs"][1]["entries"] == {
        "description": "surface of the sun",
        "temperature": 6000.0,
        "state": "gas",
        "safe": False,
        "timestamp": datetime(1993, 3, 22, tzinfo=timezone.utc).isoformat(),
    }

    # temperature == -210.0
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "temperature == -210.0"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 1
    assert isinstance(result["logs"][0]["ts"], str)
    assert result["logs"][0]["entries"] == {
        "description": "freezing nitrogen",
        "temperature": -210.0,
        "state": "liquid->solid",
        "safe": False,
        "timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
    }

    # timestamp later than 23/03/1993
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": 'timestamp > "1993-03-23"'},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 3
    assert result["logs"][0]["entries"] == {
        "description": "lava",
        "metadata": [1, 5, 6],
        "_data": {"a": 2, "b": 4},
        "timestamp": (datetime(1993, 3, 24, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][1]["entries"] == {
        "description": "air",
        "metadata": [3, 8, 5],
        "_data": {"a": 6, "b": 12, "c": 8, "d": 11},
        "timestamp": (datetime(1993, 3, 24, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][2]["entries"] == {
        "_data": {"a": 8, "b": 10},
        "timestamp": (datetime(1993, 3, 24, tzinfo=timezone.utc)).isoformat(),
    }

    # timestamp earlier than 23/03/1993
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": 'timestamp < "1993-03-23"'},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 4
    assert result["logs"][0]["entries"] == {
        "description": "boiling water",
        "temperature": 100.0,
        "state": "liquid->gas",
        "safe": False,
        "timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][1]["entries"] == {
        "description": "freezing water",
        "temperature": 0.0,
        "state": "liquid->solid",
        "safe": True,
        "timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][2]["entries"] == {
        "description": "surface of the sun",
        "temperature": 6000.0,
        "state": "gas",
        "safe": False,
        "timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
    }
    assert result["logs"][3]["entries"] == {
        "description": "freezing nitrogen",
        "temperature": -210.0,
        "state": "liquid->solid",
        "safe": False,
        "timestamp": (datetime(1993, 3, 22, tzinfo=timezone.utc)).isoformat(),
    }

    # timestamp is 23/03/1993
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": 'timestamp == "1993-03-23"'},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 0

    # is earlier than or later than 23/03/1993
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": 'timestamp < "1993-03-23" or timestamp > "1993-03-23"'},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 7

    # safe is True
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "safe is True"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 1
    assert result["logs"][0]["entries"] == {
        "description": "freezing water",
        "temperature": 0.0,
        "state": "liquid->solid",
        "safe": True,
        "timestamp": datetime(1993, 3, 22, tzinfo=timezone.utc).isoformat(),
    }

    # liquid not in state
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "'liquid' not in state"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 1
    assert result["logs"][0]["entries"] == {
        "description": "surface of the sun",
        "temperature": 6000.0,
        "state": "gas",
        "safe": False,
        "timestamp": datetime(1993, 3, 22, tzinfo=timezone.utc).isoformat(),
    }

    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "description == 'boiling water'"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 1
    assert result["logs"][0]["entries"]["description"] == "boiling water"

    # check multiple conditions
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "('liquid' not in state) or (temperature == 0)"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 2
    assert result["logs"][0]["entries"] == {
        "description": "freezing water",
        "temperature": 0.0,
        "state": "liquid->solid",
        "safe": True,
        "timestamp": datetime(1993, 3, 22, tzinfo=timezone.utc).isoformat(),
    }
    assert result["logs"][1]["entries"] == {
        "description": "surface of the sun",
        "temperature": 6000.0,
        "state": "gas",
        "safe": False,
        "timestamp": datetime(1993, 3, 22, tzinfo=timezone.utc).isoformat(),
    }

    # check exists
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "exists(state)"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 4

    # check not exists
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "not exists(temperature)"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 3

    # check len
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "len(description) < 10"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 2
    assert result["logs"][0]["entries"]["description"] == "lava"

    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "len(_data) > 2"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 1
    assert result["logs"][0]["entries"]["description"] == "air"

    # check in
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "'lava' in description"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 1
    assert result["logs"][0]["entries"]["description"] == "lava"

    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "version('a/b/param1') == '1'"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result["logs"]) == 1
    assert result["logs"][0]["params"]["a/b/param1"] == "1"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "key",
    ["description", "temperature", "safe", "metadata", "_data"],
)
@pytest.mark.parametrize(
    "metric",
    ["sum", "mean", "var", "std", "min", "max", "median", "mode"],
)
@pytest.mark.parametrize(
    "log_ids",
    [[1, 3, 5, 6], None],
)
async def test_get_logs_metric(
    client: AsyncClient,
    key: str,
    metric: str,
    log_ids: Optional[List[int]],
):
    project_name = "eval-project"
    _ = await _create_project(client, project_name)
    _ = await _create_logs_for_filtering_n_metrics(client, project_name)
    data = log_data["logs_for_filtering_n_metrics"]
    params = {} if log_ids is None else {"log_ids": log_ids}
    response = await client.get(
        f"/v0/logs/metric/{metric}/{key}?project={project_name}",
        params=params,
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    vals = [
        d[key]
        for i, d in enumerate(data)
        if key in d and (log_ids is None or i + 1 in log_ids)
    ]
    if metric == "mode" and len(set(vals)) == len(vals):
        return
    assert result == reduction_methods[metric](vals)


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

    log_id1 = response1.json()
    log_id2 = response2.json()
    log_ids = [log_id1, log_id2]

    # Delete the logs
    response = await _delete_logs(client, log_ids)
    assert response.status_code == 200, response.json()
    assert response.json()["info"] == "Logs deleted successfully!"

    # Verify logs were deleted
    response = await _get_log(client, log_id1)
    assert response.status_code == 404, response.json()

    response = await _get_log(client, log_id2)
    assert response.status_code == 404, response.json()


@pytest.mark.anyio
async def test_update_logs(client: AsyncClient):
    project_name = "multi-log-project"
    _ = await _create_project(client, project_name)

    # Create multiple logs
    response1 = await _create_log(client, project_name)
    response2 = await _create_log(client, project_name)
    assert response1.status_code == 200, response1.json()
    assert response2.status_code == 200, response2.json()

    log_id1 = response1.json()
    log_id2 = response2.json()
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
    response = await _get_log(client, log_id1)
    assert response.status_code == 200, response.json()
    assert response.json()["logs"]["entries"]["new_entry"] == "Updated value"

    response = await _get_log(client, log_id2)
    assert response.status_code == 200, response.json()
    assert response.json()["logs"]["entries"]["new_entry"] == "Updated value"


@pytest.mark.anyio
async def test_update_logs_multi_values(client: AsyncClient):
    project_name = "multi-log-project"
    _ = await _create_project(client, project_name)

    # Create multiple logs
    response1 = await _create_log(client, project_name)
    response2 = await _create_log(client, project_name)
    assert response1.status_code == 200, response1.json()
    assert response2.status_code == 200, response2.json()

    log_id1 = response1.json()
    log_id2 = response2.json()
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
    response = await _get_log(client, log_id1)
    assert response.status_code == 200, response.json()
    assert response.json()["logs"]["entries"]["new_entry"] == "First updated value"

    response = await _get_log(client, log_id2)
    assert response.status_code == 200, response.json()
    assert response.json()["logs"]["entries"]["new_entry"] == "Second updated value"


@pytest.mark.anyio
async def test_delete_log_field_from_logs(client: AsyncClient):
    project_name = "multi-log-project"
    _ = await _create_project(client, project_name)

    # Create multiple logs
    response1 = await _create_log(client, project_name)
    response2 = await _create_log(client, project_name)
    assert response1.status_code == 200, response1.json()
    assert response2.status_code == 200, response2.json()

    log_id1 = response1.json()
    log_id2 = response2.json()
    log_ids = [log_id1, log_id2]

    # Delete an entry from both logs
    entry_to_delete = "a/b/c/input"
    response = await _delete_log_field_from_logs(client, entry_to_delete, log_ids)
    assert response.status_code == 200, response.json()
    assert response.json()["info"] == "Log field deleted successfully from all logs!"

    # Verify deletion of entry
    response = await _get_log(client, log_id1)
    assert response.status_code == 200, response.json()
    assert entry_to_delete not in response.json()["logs"]["entries"]

    response = await _get_log(client, log_id2)
    assert response.status_code == 200, response.json()
    assert entry_to_delete not in response.json()["logs"]["entries"]


if __name__ == "__main__":
    pass
