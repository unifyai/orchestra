import json
import os

import pytest
from httpx import AsyncClient

from ..web.api.log.helpers import (
    # evaluate_filter_expression,
    reduction_methods,
    str_filter_exp_to_dict,
)

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
        "input": "Some input data",
        "boolean_input": True,
        "numeric_input": 4.5,
    },
    "log_update": {
        "my_list": ["a", "b", "c"],
        "my_dict": {"a": 1, "b": 2, "c": 3},
    },
    "log_update_w_overwrite": {
        "boolean_input": False,
        "numeric_input": 5.4,
    },
    "logs_for_grouping": [
        {
            "input": "What is 1 + 1?",
            "system_prompt": "You are an expert mathematician.",
        },
        {
            "input": "What is 2 + 2?",
            "system_prompt": "You are an expert mathematician.",
        },
        {
            "input": "What is 1 + 1?",
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
        },
        {
            "description": "freezing water",
            "temperature": 0.0,
            "state": "liquid->solid",
            "safe": True,
        },
        {
            "description": "surface of the sun",
            "temperature": 6000.0,
            "state": "gas",
            "safe": False,
        },
        {
            "description": "freezing nitrogen",
            "temperature": -210.0,
            "state": "liquid->solid",
            "safe": False,
        },
        {"description": "lava", "metadata": [1, 5, 6], "_data/1": {1: 2, 3: 4}},
        {
            "description": "air",
            "metadata": [3, 8, 5],
            "_data/2": {5: 6, "cat": "mouse", 7: 8},
        },
    ],
}


def _create_log(client, project_name, user=1):
    _headers = HEADERS if user == 1 else HEADERS_2
    return client.post(
        "/v0/log",
        json={"project": project_name, "entries": log_data["log"]},
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


def _update_log_w_overwrite(client, log_id, user=1):
    _headers = HEADERS if user == 1 else HEADERS_2
    return client.put(
        f"/v0/log/{log_id}",
        json={"entries": log_data["log_update_w_overwrite"]},
        headers=_headers,
    )


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
            json={"project": project_name, "entries": data[i]},
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
async def test_create_logs_project_not_found(client: AsyncClient):
    project_name = "non_existent_project"

    response = await _create_log(client, project_name)

    assert response.status_code == 404, response.json()
    assert response.json() == {"detail": "Project not found."}


@pytest.mark.anyio
async def test_update_log(client: AsyncClient):
    project_name = "eval-project"
    _ = await _create_project(client, project_name)

    response = await _create_log(client, project_name)
    assert response.status_code == 200, response.json()
    log_id = response.json()
    assert isinstance(log_id, int)

    response = await _get_log(client, log_id)
    assert response.status_code == 200, response.json()
    log = response.json()
    assert len(log["entries"]) == 3

    response = await _update_log(client, log_id)
    assert response.status_code == 200, response.json()

    response = await _get_log(client, log_id)
    assert response.status_code == 200, response.json()
    log = response.json()
    assert len(log["entries"]) == 5


@pytest.mark.anyio
async def test_update_log_overwrites(client: AsyncClient):
    project_name = "eval-project"
    _ = await _create_project(client, project_name)

    response = await _create_log(client, project_name)
    assert response.status_code == 200, response.json()
    log_id = response.json()
    assert isinstance(log_id, int)

    response = await _get_log(client, log_id)
    assert response.status_code == 200, response.json()
    orig_entries = response.json()["entries"]
    assert len(orig_entries) == 3

    response = await _update_log_w_overwrite(client, log_id)
    assert response.status_code == 200, response.json()

    response = await _get_log(client, log_id)
    assert response.status_code == 200, response.json()
    new_entries = response.json()["entries"]
    assert len(new_entries) == 3
    assert new_entries["input"] == orig_entries["input"]
    assert new_entries["boolean_input"] != orig_entries["boolean_input"]
    assert new_entries["numeric_input"] != orig_entries["numeric_input"]


@pytest.mark.anyio
async def test_update_log_not_found(client: AsyncClient):
    non_existent_log_id = 1234
    response = await _update_log(client, non_existent_log_id)
    assert response.status_code == 404, response.json()
    assert response.json() == {
        "detail": f"Log with id {non_existent_log_id} not found.",
    }


@pytest.mark.anyio
async def test_delete_log(client: AsyncClient):
    project_name = "eval-project"
    _ = await _create_project(client, project_name)
    log_response = await _create_log(client, project_name)
    log_id = log_response.json()

    # delete the log
    response = await client.delete(f"/v0/log/{log_id}", headers=HEADERS)

    assert response.status_code == 200, response.json()
    assert response.json() == {"info": "Log deleted successfully!"}

    # TODO: Try to fetch the deleted log


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
async def test_delete_log_not_found(client: AsyncClient):
    log_id = "123"

    # This should return 404 as the log does not exist
    response = await client.delete(f"/v0/log/{log_id}", headers=HEADERS)

    assert response.status_code == 404, response.json()
    assert response.json() == {
        "detail": f"Log with id {log_id} not found.",
    }


@pytest.mark.anyio
async def test_delete_log_entry(client: AsyncClient):
    project_name = "eval-project"
    _ = await _create_project(client, project_name)
    log_response = await _create_log(client, project_name)
    log_id = log_response.json()

    # delete an entry in the log
    response = await client.delete(f"/v0/log/{log_id}/entry/input", headers=HEADERS)

    assert response.status_code == 200, response.json()
    assert response.json() == {"info": "Log entry deleted successfully!"}

    # TODO: Fetch before and after to check entries


@pytest.mark.anyio
async def test_delete_log_entry_not_found(client: AsyncClient):
    log_id = "123"

    # This should return 404 as the log entry does not exist
    response = await client.delete(f"/v0/log/{log_id}/entry/input", headers=HEADERS)

    assert response.status_code == 404, response.json()
    assert response.json() == {
        "detail": f"Log with id {log_id} not found.",
    }

    # TODO: There are a couple more exceptions not being tested I think


@pytest.mark.anyio
async def test_get_log(client: AsyncClient):
    project_name = "eval-project"
    _ = await _create_project(client, project_name)
    log_response = await _create_log(client, project_name)
    log_id = log_response.json()

    # fetch the log
    response = await client.get(f"/v0/log/{log_id}", headers=HEADERS)

    assert response.status_code == 200, response.json()
    assert "entries" in response.json()  # Log entries are returned
    assert isinstance(response.json()["ts"], str)
    assert isinstance(response.json()["entries"]["boolean_input"], bool)
    assert isinstance(response.json()["entries"]["numeric_input"], float)


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
        (
            "(messages == [{'role': 'assistant', "
            "'context': 'you are a helpful assistant'}])",
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
def test_log_filter_helper(expression, values):
    express_dict = str_filter_exp_to_dict(expression)
    assert isinstance(express_dict, dict)
    result = evaluate_filter_expression(express_dict, **values)
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
    assert isinstance(response.json(), list)  # List of logs is returned
    assert isinstance(response.json()[0]["ts"], str)
    assert isinstance(response.json()[0]["entries"]["boolean_input"], bool)
    assert isinstance(response.json()[0]["entries"]["numeric_input"], float)

    # fetch entries for the empty project
    response = await client.get(f"/v0/logs?project={project_name}", headers=HEADERS_2)

    assert response.status_code == 200, response.json()
    assert isinstance(response.json(), list)  # List of logs is returned
    assert len(response.json()) == 0


@pytest.mark.anyio
async def test_get_empty_logs(client: AsyncClient):
    project_name = "eval-project"
    _ = await _create_project(client, project_name)

    # fetch entries for the project
    response = await client.get(f"/v0/logs?project={project_name}", headers=HEADERS)

    assert response.status_code == 200, response.json()
    assert isinstance(response.json(), list)  # List of logs is returned
    assert len(response.json()) == 0  # Logs are empty


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
    assert len(result) == 2
    assert isinstance(result[0]["ts"], str)
    assert isinstance(result[1]["ts"], str)
    assert result[0]["entries"] == {
        "description": "boiling water",
        "temperature": 100.0,
        "state": "liquid->gas",
        "safe": False,
    }
    assert result[1]["entries"] == {
        "description": "surface of the sun",
        "temperature": 6000.0,
        "state": "gas",
        "safe": False,
    }

    # safe is True
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "safe is True"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result) == 1
    assert result[0]["entries"] == {
        "description": "freezing water",
        "temperature": 0.0,
        "state": "liquid->solid",
        "safe": True,
    }

    # liquid not in state
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "'liquid' not in state"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result) == 1
    assert result[0]["entries"] == {
        "description": "surface of the sun",
        "temperature": 6000.0,
        "state": "gas",
        "safe": False,
    }

    # check multiple conditions
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "('liquid' not in state) or (temperature == 0)"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result) == 2
    assert result[0]["entries"] == {
        "description": "freezing water",
        "temperature": 0.0,
        "state": "liquid->solid",
        "safe": True,
    }
    assert result[1]["entries"] == {
        "description": "surface of the sun",
        "temperature": 6000.0,
        "state": "gas",
        "safe": False,
    }

    # check exists
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "exists(state)"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result) == 4

    # check not exists
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "not exists(temperature)"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result) == 2

    # check len
    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "len(description) < 10"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result) == 2
    assert result[0]["entries"]["description"] == "lava"

    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "len(_data) > 2"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result) == 1
    assert result[0]["entries"]["description"] == "air"

    # check in

    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "'lava' in description"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result) == 1
    assert result[0]["entries"]["description"] == "lava"

    response = await client.get(
        f"/v0/logs?project={project_name}",
        params={"filter_expr": "version('_data') == '2'"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert len(result) == 1
    assert result[0]["entries"]["description"] == "air"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "key",
    ["description", "temperature", "state", "safe", "metadata", "_data"],
)
@pytest.mark.parametrize(
    "metric",
    ["sum", "mean", "var", "std", "min", "max", "median", "mode"],
)
async def test_get_logs_metric(client: AsyncClient, key: str, metric: str):
    project_name = "eval-project"
    _ = await _create_project(client, project_name)
    _ = await _create_logs_for_filtering_n_metrics(client, project_name)
    data = log_data["logs_for_filtering_n_metrics"]
    response = await client.get(
        f"/v0/logs/metric/{metric}/{key}?project={project_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert result == reduction_methods[metric]([d[key] for d in data if key in d])


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
