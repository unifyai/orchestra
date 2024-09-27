import asyncio
import os
import time

from httpx import AsyncClient

import orchestra
from .test_evaluation import _seed_evaluations_db


## UTILS

api_key = str(os.getenv("AUTH_ACCOUNT_API_KEY"))
HEADERS = {
    "accept": "application/json",
    "Authorization": f"Bearer {api_key}",
}


## TESTS


async def test_train_router(client: AsyncClient, monkeypatch, dbsession):

    # mocking pubsub
    def mock_send_to_train_server(action, **data):
        assert "user_id" in data
        assert "api_key" in data
        data.pop("user_id")
        data.pop("api_key")
        assert data == {
            # "user_id": "XXX",
            # "api_key": "XXX",
            "prompt_ids": [1, 2, 3],
            "router_id": 1,
            "endpoints": [
                "llama-3-8b-chat@aws-bedrock",
                "llama-3-70b-chat@aws-bedrock",
            ],
            "evaluator_id": 1,
        }

    monkeypatch.setattr(
        orchestra.web.api.router_training.views,
        "send_to_train_server",
        mock_send_to_train_server,
    )

    ### test begins
    await _seed_evaluations_db(
        dbsession,
        path="./orchestra/tests/sql_dumps/evaluations/dump_router_train.jsonl",
    )

    url = "/v0/router/train"
    params = {
        "name": "my_test_router",
        "prompts": "1,2,3",
        "endpoints": ["llama-3-8b-chat@aws-bedrock", "llama-3-70b-chat@aws-bedrock"],
        "evaluator": "test_eval",
    }
    response = await client.post(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()
    assert response.json() == {
        "info": "Router training started! You will receive an email soon!"
    }


# provisionally testing all three endpoints in a single test
async def test_train_delete_router(client: AsyncClient, dbsession):
    await _seed_evaluations_db(
        dbsession,
        path="./orchestra/tests/sql_dumps/evaluations/dump_trained_routers.jsonl",
    )
    url = "/v0/router"
    params = {"name": "my_test_router"}
    response = await client.delete(url, params=params, headers=HEADERS)

    url = "/v0/router/list"
    response = await client.get(url, headers=HEADERS)
    assert response.status_code == 200, repsonse.json()
    print(response.json())
    assert response.json() == ""


async def test_list_router(client: AsyncClient, dbsession):
    await _seed_evaluations_db(
        dbsession,
        path="./orchestra/tests/sql_dumps/evaluations/dump_trained_routers.jsonl",
    )
    url = "/v0/router/list"
    response = await client.get(url, headers=HEADERS)
    assert response.status_code == 200, repsonse.json()
    print(response.json())
    assert response.json() == ""


async def test_rename_router(client: AsyncClient, dbsession):
    await _seed_evaluations_db(
        dbsession,
        path="./orchestra/tests/sql_dumps/evaluations/dump_trained_routers.jsonl",
    )
    url = "/v0/router/rename"
    params = {"name": "my_test_router", "new_name": "my_new_test_router"}
    response = await client.post(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()


###


async def test_deploy_router(client: AsyncClient, monkeypatch, dbsession):

    def mock_send_to_deploy_server(action, **data):
        data.pop("user_id")
        assert data == {
            #'user_id': 'XXX',
            "router_id": 2,
        }

    monkeypatch.setattr(
        orchestra.web.api.router_deployment.views,
        "send_to_deploy_server",
        mock_send_to_deploy_server,
    )

    await _seed_evaluations_db(
        dbsession,
        path="./orchestra/tests/sql_dumps/evaluations/dump_trained_routers.jsonl",
    )

    url = "/v0/router/deploy"
    params = {"name": "my_test_router_2"}
    response = await client.post(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()


async def test_deploy_undeploy(client: AsyncClient, dbsession):
    await _seed_evaluations_db(
        dbsession,
        path="./orchestra/tests/sql_dumps/evaluations/dump_trained_routers.jsonl",
    )
    url = "/v0/router/deploy"
    response = await client.delete(url, params={"name": "my_test_router_2"}, headers=HEADERS)
    assert response.status_code == 200, response.json()
    print(response.json())

async def test_deploy_list_router(client: AsyncClient, dbsession):
    await _seed_evaluations_db(
        dbsession,
        path="./orchestra/tests/sql_dumps/evaluations/dump_trained_routers.jsonl",
    )
    url = "/v0/router/deploy/list"
    response = await client.get(url, headers=HEADERS)
    assert response.status_code == 200, response.json()
    print(response.json())
