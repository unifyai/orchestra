import os

import pytest
from httpx import AsyncClient

from ..web.api.log.helpers import (
    evaluate_filter_expression,
    reduction_methods,
    str_filter_exp_to_dict,
)

api_key = str(os.getenv("AUTH_ACCOUNT_API_KEY"))

HEADERS = {
    "accept": "application/json",
    "Authorization": f"Bearer {api_key}",
}

log_data = {
    "logs": {
        "input": "Some input data",
        "boolean_input": True,
        "numeric_input": 4.5,
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
    ],
}


def _create_logs(client, project_name):
    return client.post(
        "/v0/log",
        json={"project": project_name, "logs": log_data["logs"]},
        headers=HEADERS,
    )


async def _create_logs_for_grouping(client, project_name):
    data = log_data["logs_for_grouping"]
    for i in range(len(data)):
        response = await client.post(
            "/v0/log",
            json={"project": project_name, "logs": data[i]},
            headers=HEADERS,
        )
        assert response.status_code == 200, response.json()


async def _create_logs_for_filtering_n_metrics(client, project_name):
    data = log_data["logs_for_filtering_n_metrics"]
    for i in range(len(data)):
        response = await client.post(
            "/v0/log",
            json={"project": project_name, "logs": data[i]},
            headers=HEADERS,
        )
        assert response.status_code == 200, response.json()


def _create_project(client, project_name):
    url = "/v0/project"
    project_data = {"name": project_name}
    return client.post(url, json=project_data, headers=HEADERS)


@pytest.mark.anyio
async def test_create_logs(client: AsyncClient):
    project_name = "eval-project"
    _ = await _create_project(client, project_name)

    response = await _create_logs(client, project_name)

    assert response.status_code == 200, response.json()
    assert isinstance(response.json(), int)

    # TODO: Get log and see if it matches


@pytest.mark.anyio
async def test_create_logs_project_not_found(client: AsyncClient):
    project_name = "non_existent_project"

    response = await _create_logs(client, project_name)

    assert response.status_code == 404, response.json()
    assert response.json() == {"detail": "A project with this name doesn't exists."}


@pytest.mark.anyio
async def test_delete_log(client: AsyncClient):
    project_name = "eval-project"
    _ = await _create_project(client, project_name)
    log_response = await _create_logs(client, project_name)
    log_id = log_response.json()

    # delete the log
    response = await client.delete(f"/v0/log/{log_id}", headers=HEADERS)

    assert response.status_code == 200, response.json()
    assert response.json() == {"info": "Log deleted successfully!"}

    # TODO: Try to fetch the deleted log


@pytest.mark.anyio
async def test_delete_log_not_found(client: AsyncClient):
    log_id = "123"

    # This should return 404 as the log does not exist
    response = await client.delete(f"/v0/log/{log_id}", headers=HEADERS)

    assert response.status_code == 404, response.json()
    assert response.json() == {
        "detail": f"Log with id {log_id} not found in your account.",
    }


@pytest.mark.anyio
async def test_delete_log_entry(client: AsyncClient):
    project_name = "eval-project"
    _ = await _create_project(client, project_name)
    log_response = await _create_logs(client, project_name)
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
        "detail": f"Log with id {log_id} not found in your account.",
    }

    # TODO: There are a couple more exceptions not being tested I think


@pytest.mark.anyio
async def test_get_log(client: AsyncClient):
    project_name = "eval-project"
    _ = await _create_project(client, project_name)
    log_response = await _create_logs(client, project_name)
    log_id = log_response.json()

    # fetch the log
    response = await client.get(f"/v0/log/{log_id}", headers=HEADERS)

    assert response.status_code == 200, response.json()
    assert "entries" in response.json()  # Log entries are returned
    assert isinstance(response.json()["entries"]["boolean_input"], bool)
    assert isinstance(response.json()["entries"]["numeric_input"], float)


@pytest.mark.anyio
async def test_get_log_not_found(client: AsyncClient):
    log_id = "123"

    # This should return 404 as the log does not exist
    response = await client.get(f"/v0/log/{log_id}", headers=HEADERS)

    assert response.status_code == 404, response.json()
    assert response.json() == {
        "detail": f"Log with id {log_id} not found in your account.",
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
    ],
)
def test_log_filter_helper(expression, values):
    express_dict = str_filter_exp_to_dict(expression)
    assert isinstance(express_dict, dict)
    result = evaluate_filter_expression(express_dict, **values)
    for key, value in values.items():
        exec(
            key
            + "="
            + ('"{}"'.format(value) if isinstance(value, str) else str(value)),
        )
    expected = eval(expression)
    assert result == expected


@pytest.mark.anyio
async def test_get_logs(client: AsyncClient):
    project_name = "eval-project"
    _ = await _create_project(client, project_name)
    _ = await _create_logs(client, project_name)

    # fetch logs for the project
    response = await client.get(f"/v0/logs?project={project_name}", headers=HEADERS)

    assert response.status_code == 200, response.json()
    assert isinstance(response.json(), list)  # List of logs is returned
    assert isinstance(response.json()[0]["entries"]["boolean_input"], bool)
    assert isinstance(response.json()[0]["entries"]["numeric_input"], float)


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


@pytest.mark.anyio
@pytest.mark.parametrize("key", ["description", "temperature", "state", "safe"])
@pytest.mark.parametrize(
    "metric",
    ["sum", "mean", "var", "std", "min", "max", "median", "mode"],
)
async def test_get_log_metrics(client: AsyncClient, key: str, metric: str):
    project_name = "eval-project"
    _ = await _create_project(client, project_name)
    _ = await _create_logs_for_filtering_n_metrics(client, project_name)
    data = log_data["logs_for_filtering_n_metrics"]
    response = await client.get(
        f"/v0/logs/metrics?project={project_name}",
        headers=HEADERS,
        params={"key": key, "metric": metric},
    )
    assert response.status_code == 200, response.json()
    result = response.json()
    assert result == reduction_methods[metric]([d[key] for d in data])


@pytest.mark.anyio
async def test_get_logs_project_not_found(client: AsyncClient):
    project_name = "non_existent_project"

    # This should return 404 as the project does not exist
    response = await client.get(f"/v0/logs?project={project_name}", headers=HEADERS)

    assert response.status_code == 404, response.json()
    assert response.json() == {
        "detail": f"Project {project_name} not found in your account.",
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
    assert isinstance(groups, dict)  # Ensure it's a dict of grouped entries
    assert len(groups) == 2
    assert groups == {
        "0": '"You are an expert mathematician."',
        "1": '"Respond only with a single digit."',
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
        "detail": f"Project {project_name} not found in your account.",
    }
