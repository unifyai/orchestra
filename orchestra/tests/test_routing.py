import asyncio
import json
import os
import sys

import pytest
from httpx import AsyncClient

import orchestra

from .test_evaluation import _seed_evaluations_db

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, project_root)
train_router_path = os.path.join(project_root, "train_router")
sys.path.insert(0, train_router_path)
from train_router.router_training import train_router

deploy_router_path = os.path.join(project_root, "deploy_router")
sys.path.insert(0, deploy_router_path)
from deploy_router.router_deployment import undeploy_router

## UTILS

api_key = str(os.getenv("AUTH_ACCOUNT_API_KEY"))
HEADERS = {
    "accept": "application/json",
    "Authorization": f"Bearer {api_key}",
}


## TESTS


async def test_train_router_pre_pubsub(client: AsyncClient, monkeypatch, dbsession):

    # mocking pubsub
    def mock_send_to_train_server(action, **data):
        assert "user_id" in data
        assert "api_key" in data
        data.pop("user_id")
        data.pop("api_key")
        assert data == {
            # "user_id": "XXX",
            # "api_key": "XXX",
            "datum_ids": [1, 2, 3],
            "router_id": 1,
            "endpoints": [
                "llama-3-8b-chat@aws-bedrock",
                "llama-3-70b-chat@aws-bedrock",
            ],
            "evaluator": "test_eval",
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
        "info": "Router training started! You will receive an email soon!",
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
    assert response.json() == {"info": "Trained router deleted!"}

    url = "/v0/router/list"
    response = await client.get(url, headers=HEADERS)
    assert response.status_code == 200, repsonse.json()
    assert response.json() == ["my_test_router_2", "my_test_router_3"]


async def test_list_router(client: AsyncClient, dbsession):
    await _seed_evaluations_db(
        dbsession,
        path="./orchestra/tests/sql_dumps/evaluations/dump_trained_routers.jsonl",
    )
    url = "/v0/router/list"
    response = await client.get(url, headers=HEADERS)
    assert response.status_code == 200, repsonse.json()
    assert response.json() == ["my_test_router", "my_test_router_2", "my_test_router_3"]


async def test_rename_router(client: AsyncClient, dbsession):
    await _seed_evaluations_db(
        dbsession,
        path="./orchestra/tests/sql_dumps/evaluations/dump_trained_routers.jsonl",
    )
    url = "/v0/router/rename"
    params = {"name": "my_test_router", "new_name": "my_new_test_router"}
    response = await client.post(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()


#####

# I don't think this test "works" on the CI (probably for pubsub permissions), but it works locally.
async def test_train_router_e2e(client: AsyncClient, monkeypatch, tmp_path, dbsession):
    # mocking pubsub
    def mock_send_to_train_server(action, **data):
        data.pop("user_email", "")
        message_data = json.dumps(
            {
                "action": action,
                **data,
                "orchestra_url": "https://api.unify.ai/v0",
                "admin_key": os.environ.get("ORCHESTRA_ADMIN_KEY"),
            },
        )
        save_dir = tmp_path / "save_files"
        if action == "train":
            asyncio.run(
                train_router(
                    message_data,
                    save_dir,
                    client=client,
                ),
            )
        else:
            raise NotImplementedError

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
        "prompts": "1,2,3,4,5,6,7,8,9",
        "endpoints": ["llama-3-8b-chat@aws-bedrock", "gpt-3.5-turbo@openai"],
        "evaluator": "test_eval",
    }
    response = await client.post(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()
    assert response.json() == {
        "info": "Router training started! You will receive an email soon!",
    }


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


@pytest.mark.skip(
    reason="can't actually test undeploying without waiting for it to deploy (~45mins)",
)
async def test_deploy_undeploy(client: AsyncClient, monkeypatch, dbsession):
    await _seed_evaluations_db(
        dbsession,
        path="./orchestra/tests/sql_dumps/evaluations/dump_trained_routers.jsonl",
    )

    def mock_send_to_deploy_server(action, **data):
        data.pop("user_email", "")
        message_data = json.dumps(
            {
                "action": action,
                **data,
                "orchestra_url": "https://api.unify.ai/v0",
                "admin_key": os.environ.get("ORCHESTRA_ADMIN_KEY"),
            },
        )
        if action == "undeploy":
            asyncio.run(
                undeploy_router(
                    message_data,
                ),
            )
        else:
            raise NotImplementedError

    monkeypatch.setattr(
        orchestra.web.api.router_deployment.views,
        "send_to_deploy_server",
        mock_send_to_deploy_server,
    )
    url = "/v0/router/deploy"
    response = await client.delete(
        url,
        params={"name": "my_test_router_3"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()


async def test_deploy_list_router(client: AsyncClient, dbsession):
    await _seed_evaluations_db(
        dbsession,
        path="./orchestra/tests/sql_dumps/evaluations/dump_trained_routers.jsonl",
    )
    url = "/v0/router/deploy/list"
    response = await client.get(url, headers=HEADERS)
    assert response.status_code == 200, response.json()
    print(response.json())


## test doesn't work
# async def test_msg_router(client: AsyncClient, dbsession):
#     await _seed_evaluations_db(
#         dbsession,
#         path="./orchestra/tests/sql_dumps/evaluations/dump_trained_routers.jsonl",
#     )
#     endpoint = "/v0/chat/completions"
#     data = get_chat_completions_payload(model="router_my_test_router_3", provider="q:1", stream=False)
#     response = await client.post(endpoint, headers=HEADERS, json=data)
#     assert response.status_code == 200, response.json()
