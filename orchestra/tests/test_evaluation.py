import asyncio
import json
import os
import sys

import pytest
from dotenv import find_dotenv, load_dotenv
from httpx import AsyncClient
from sqlalchemy import text

import orchestra

from .test_datasets import upload_dataset

# TODO: Less hacky way for this?
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, project_root)
dataset_eval_path = os.path.join(project_root, "dataset_evaluation")
sys.path.insert(0, dataset_eval_path)
from dataset_evaluation.evaluate_dataset import evaluate_dataset

api_key = str(os.getenv("AUTH_ACCOUNT_API_KEY"))
test_user_id = os.getenv("AUTH_ACCOUNT_USER_ID")

HEADERS = {
    "accept": "application/json",
    "Authorization": f"Bearer {api_key}",
}

# makes the tests run async
pytestmark = pytest.mark.anyio


def create_default_prompt(client, default_prompt_name):
    data = {"name": default_prompt_name, "prompt": {"temperature": 0.8}}
    # Send POST request to the /dataset endpoint
    return client.post("/v0/default_prompt", headers=HEADERS, json=data)


def _upload_dataset(client, dataset_name, data_path):
    data = {"name": dataset_name}
    with open(data_path, "rb") as f:
        file_content = f.read()
    files = {"file": ("test.jsonl", file_content, "application/x-jsonlines")}
    response = client.post("/v0/dataset", data=data, files=files, headers=HEADERS)
    return response


def _delete_dataset_evaluation(client, dataset_name):
    response = client.delete(
        "/v0/dataset",
        params={"name": dataset_name},
        headers=HEADERS,
    )
    return response


load_dotenv(find_dotenv())


async def test_trigger_eval(
    client: AsyncClient,
    tmp_path,
    monkeypatch,
):
    def mock_send_to_dataset_evaluation_server(action, **data):
        data.pop("user_email", "")
        message_data = json.dumps(
            {
                "action": action,
                **data,
                "orchestra_url": "",
                "admin_key": os.environ.get("ORCHESTRA_ADMIN_KEY"),
            },
        )
        save_dir = tmp_path / "save_files"
        if action == "evaluate":
            asyncio.run(
                evaluate_dataset(
                    message_data,
                    save_dir,
                    shared_volume="",
                    client=client,
                ),
            )
        elif action == "refresh_scores":
            user_id = data["user_id"]
            asyncio.run(refresh_scores_for_user(user_id, save_dir))
        else:
            raise NotImplementedError

    monkeypatch.setattr(
        orchestra.web.api.evaluations.views,
        "send_to_dataset_evaluation_server",
        mock_send_to_dataset_evaluation_server,
    )

    # create evaluator
    eval_name = "test_eval"
    system_prompt = "dummy system prompt"
    judge_model = "llama-3-8b-chat@aws-bedrock"

    url = "/v0/evaluator"
    params = {
        "name": eval_name,
        "system_prompt": system_prompt,
        "judge_models": judge_model,
    }
    response = await client.post(url, json=params, headers=HEADERS)
    assert response.status_code == 200, response.json()

    # create dataset

    file_path = "./orchestra/tests/sample_datasets/prompts_with_kws.jsonl"
    dataset = "test_dataset_eval"
    response = await upload_dataset(client, file_path, dataset)
    assert response.status_code == 200, response.json()

    # create trigger evaluation
    url = "/v0/evaluation"
    endpoint = "gpt-3.5-turbo@openai"
    params = {
        "url": url,
        "dataset": dataset,
        "endpoint": endpoint,
        "evaluator": eval_name,
    }
    response = await client.post(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()

    url = "/v0/evaluation"
    endpoint = "llama-3-8b-chat@aws-bedrock"
    params = {
        "url": url,
        "dataset": dataset,
        "endpoint": endpoint,
        "evaluator": eval_name,
    }
    response = await client.post(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()
    ############################

    url = "/v0/evaluation"
    params = {"dataset": dataset, "evaluator": eval_name}
    response = await client.get(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()
    scores = response.json()
    assert eval_name in scores
    assert endpoint in scores[eval_name]
    assert "score" in scores[eval_name][endpoint]
    assert "progress" in scores[eval_name][endpoint]
    #################################
    # per prompt
    url = "/v0/evaluation"
    params = {
        "dataset": dataset,
        "evaluator": eval_name,
        "endpoint": endpoint,
        "per_prompt": True,
    }
    response = await client.get(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()
    scores = response.json()
    assert eval_name in scores
    assert endpoint in scores[eval_name]
    assert "score" in scores[eval_name][endpoint]
    assert "progress" in scores[eval_name][endpoint]
    assert "per_prompt" in scores[eval_name][endpoint]


# TODO: Parametrise this test to use mostly the same code as above
async def test_trigger_eval_with_default_prompt(
    client: AsyncClient,
    tmp_path,
    monkeypatch,
):
    def mock_send_to_dataset_evaluation_server(action, **data):
        data.pop("user_email", "")
        message_data = json.dumps(
            {
                "action": action,
                **data,
                "orchestra_url": "",
                "admin_key": os.environ.get("ORCHESTRA_ADMIN_KEY"),
            },
        )
        save_dir = tmp_path / "save_files"
        if action == "evaluate":
            asyncio.run(
                evaluate_dataset(
                    message_data,
                    save_dir,
                    shared_volume="",
                    client=client,
                ),
            )
        elif action == "refresh_scores":
            user_id = data["user_id"]
            asyncio.run(refresh_scores_for_user(user_id, save_dir))
        else:
            raise NotImplementedError

    monkeypatch.setattr(
        orchestra.web.api.evaluations.views,
        "send_to_dataset_evaluation_server",
        mock_send_to_dataset_evaluation_server,
    )

    # create evaluator
    eval_name = "test_eval_dp"
    default_prompt_name = "dp_1"
    system_prompt = "dummy system prompt"
    judge_model = "llama-3-8b-chat@aws-bedrock"

    url = "/v0/evaluator"
    params = {
        "name": eval_name,
        "system_prompt": system_prompt,
        "judge_models": judge_model,
    }
    response = await client.post(url, json=params, headers=HEADERS)
    assert response.status_code == 200, response.json()

    # create dataset

    file_path = "./orchestra/tests/sample_datasets/prompts_with_kws.jsonl"
    dataset = "test_dataset_eval"
    response = await upload_dataset(client, file_path, dataset)
    assert response.status_code == 200, response.json()

    # create default prompt
    response = await create_default_prompt(client, default_prompt_name)
    assert response.status_code == 200, response.json()

    # create trigger evaluation
    url = "/v0/evaluation"
    endpoint = "gpt-3.5-turbo@openai"
    params = {
        "url": url,
        "dataset": dataset,
        "endpoint": endpoint,
        "evaluator": eval_name,
        "default_prompt": default_prompt_name,
    }
    response = await client.post(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()

    url = "/v0/evaluation"
    endpoint = "llama-3-8b-chat@aws-bedrock"
    params = {
        "url": url,
        "dataset": dataset,
        "endpoint": endpoint,
        "evaluator": eval_name,
        "default_prompt": default_prompt_name,
    }
    response = await client.post(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()
    ############################

    url = "/v0/evaluation"
    params = {
        "dataset": dataset,
        "evaluator": eval_name,
        "default_prompt": default_prompt_name,
    }
    response = await client.get(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()
    scores = response.json()
    assert eval_name in scores
    assert endpoint in scores[eval_name]
    assert "score" in scores[eval_name][endpoint]
    assert "progress" in scores[eval_name][endpoint]
    #################################
    # per prompt
    url = "/v0/evaluation"
    params = {
        "dataset": dataset,
        "evaluator": eval_name,
        "endpoint": endpoint,
        "per_prompt": True,
        "default_prompt": default_prompt_name,
    }
    response = await client.get(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()
    scores = response.json()
    assert eval_name in scores
    assert endpoint in scores[eval_name]
    assert "score" in scores[eval_name][endpoint]
    assert "progress" in scores[eval_name][endpoint]
    assert "per_prompt" in scores[eval_name][endpoint]


async def test_client_side_scores(
    client: AsyncClient,
    tmp_path,
):
    eval_name = "test_eval_clientside"

    # create test dataset
    file_path = "./orchestra/tests/sample_datasets/prompts_with_kws.jsonl"
    dataset = "test_dataset"
    response = await upload_dataset(client, file_path, dataset)
    assert response.status_code == 200, response.json()

    url = "/v0/evaluator"
    params = {
        "name": eval_name,
        "client_side": True,
    }
    response = await client.post(url, json=params, headers=HEADERS)
    assert response.status_code == 200, response.json()

    url = "/v0/evaluation"
    dataset = "test_dataset"
    endpoint = "llama-3-8b-chat@aws-bedrock"
    file_path = "./orchestra/tests/sample_datasets/prompts_with_scores.jsonl"
    with open(file_path, "rb") as f:
        file_content = f.read()
    files = {
        "client_side_scores": ("test.jsonl", file_content, "application/x-jsonlines"),
    }

    params = {
        "url": url,
        "dataset": dataset,
        "endpoint": endpoint,
        "evaluator": eval_name,
    }
    response = await client.post(url, params=params, files=files, headers=HEADERS)
    assert response.status_code == 200, response.json()

    url = "/v0/evaluation"
    params = {"dataset": dataset, "evaluator": eval_name}
    response = await client.get(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()
    scores = response.json()
    assert eval_name in scores
    assert endpoint in scores[eval_name]


# helper utils


async def populate_from_file(path, session):
    with open(path) as f:
        for line in f:
            command = json.loads(line)
            statement = command["statement"]
            statement = statement.replace("%(", ":").replace(")s", "")
            session.execute(text(statement), command["parameters"])


async def get_evaluation_scores(client, params):
    url = "/v0/evaluation"
    response = await client.get(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()
    scores = response.json()
    return scores


# Listing Tests

# The database contains
# {
#     "test_eval": {
#         "gpt-3.5-turbo@openai": {"score": 100.0, "progress": 100.0},
#         "llama-3-8b-chat@aws-bedrock": {"score": 90.0, "progress": 100.0},
#     },
#     "test_eval_2": {"llama-3-8b-chat@aws-bedrock": {"score": 90.0, "progress": 100.0}},
# }


async def _seed_evaluations_db(dbsession):
    path = "./orchestra/tests/sql_dumps/evaluations/dump_trigger.jsonl"
    await populate_from_file(path=path, session=dbsession)


async def _helper_test_list_evaluations(client, params, expected_scores):
    scores = await get_evaluation_scores(client, params)
    assert scores == expected_scores


async def test_list_evaluation_evaluator_and_endpoint(client: AsyncClient, dbsession):
    await _seed_evaluations_db(dbsession)
    params = {
        "dataset": "test_dataset_eval",
        "evaluator": "test_eval",
        "endpoint": "llama-3-8b-chat@aws-bedrock",
    }
    expected_scores = {
        "test_eval": {
            "llama-3-8b-chat@aws-bedrock": {"score": 90.0, "progress": 100.0},
        },
    }
    await _helper_test_list_evaluations(client, params, expected_scores)


async def test_list_evaluation_endpoint(client: AsyncClient, dbsession):

    await _seed_evaluations_db(dbsession)

    params = {
        "dataset": "test_dataset_eval",
        "endpoint": "llama-3-8b-chat@aws-bedrock",
    }
    expected_scores = {
        "test_eval": {
            "llama-3-8b-chat@aws-bedrock": {"score": 90.0, "progress": 100.0},
        },
        "test_eval_2": {
            "llama-3-8b-chat@aws-bedrock": {"score": 90.0, "progress": 100.0}
        },
    }
    await _helper_test_list_evaluations(client, params, expected_scores)


async def test_list_evaluation_evaluator(client: AsyncClient, dbsession):
    await _seed_evaluations_db(dbsession)

    params = {
        "dataset": "test_dataset_eval",
        "evaluator": "test_eval",
    }

    expected_scores = {
        "test_eval": {
            "gpt-3.5-turbo@openai": {"score": 100.0, "progress": 100.0},
            "llama-3-8b-chat@aws-bedrock": {"score": 90.0, "progress": 100.0},
        }
    }
    await _helper_test_list_evaluations(client, params, expected_scores)


async def test_list_evaluation_all(client: AsyncClient, dbsession):
    await _seed_evaluations_db(dbsession)

    params = {
        "dataset": "test_dataset_eval",
    }

    expected_scores = {
        "test_eval": {
            "gpt-3.5-turbo@openai": {"score": 100.0, "progress": 100.0},
            "llama-3-8b-chat@aws-bedrock": {"score": 90.0, "progress": 100.0},
        },
        "test_eval_2": {
            "llama-3-8b-chat@aws-bedrock": {"score": 90.0, "progress": 100.0}
        },
    }
    await _helper_test_list_evaluations(client, params, expected_scores)


# Deletion Tests


async def test_delete_evaluation_endpoint_and_evaluator(client: AsyncClient, dbsession):

    await _seed_evaluations_db(dbsession)

    params = {
        "dataset": "test_dataset_eval",
        "evaluator": "test_eval",
        "endpoint": "llama-3-8b-chat@aws-bedrock",
    }
    # delete evaluation
    response = await client.delete(
        "/v0/evaluation",
        params=params,
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert response.json() == {
        "info": "Evaluation deleted successfully. You deleted 2 evaluations."
    }

    # check deleted
    expected_scores = {
        "test_eval": {
            "gpt-3.5-turbo@openai": {"score": 100.0, "progress": 100.0},
        },
        "test_eval_2": {
            "llama-3-8b-chat@aws-bedrock": {"score": 90.0, "progress": 100.0}
        },
    }
    all_params = {"dataset": "test_dataset_eval"}
    await _helper_test_list_evaluations(client, all_params, expected_scores)


async def test_delete_no_endpoint(client: AsyncClient, dbsession):

    await _seed_evaluations_db(dbsession)

    params = {
        "dataset": "test_dataset_eval",
        "evaluator": "test_eval",
    }

    # delete evaluation
    response = await client.delete(
        "/v0/evaluation",
        params=params,
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert response.json() == {
        "info": "Evaluation deleted successfully. You deleted 4 evaluations."
    }

    # check deleted
    expected_scores = {
        "test_eval_2": {
            "llama-3-8b-chat@aws-bedrock": {"score": 90.0, "progress": 100.0}
        },
    }
    all_params = {"dataset": "test_dataset_eval"}
    await _helper_test_list_evaluations(client, all_params, expected_scores)


async def test_delete_no_evaluator(client: AsyncClient, dbsession):

    await _seed_evaluations_db(dbsession)

    params = {
        "dataset": "test_dataset_eval",
        "endpoint": "llama-3-8b-chat@aws-bedrock",
    }

    # delete evaluation
    response = await client.delete(
        "/v0/evaluation",
        params=params,
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert response.json() == {
        "info": "Evaluation deleted successfully. You deleted 4 evaluations."
    }

    # check deleted
    expected_scores = {
        "test_eval": {
            "gpt-3.5-turbo@openai": {"score": 100.0, "progress": 100.0},
        },
    }
    all_params = {"dataset": "test_dataset_eval"}
    await _helper_test_list_evaluations(client, all_params, expected_scores)


async def test_delete_no_evaluator_no_endpoint(client: AsyncClient, dbsession):

    await _seed_evaluations_db(dbsession)

    params = {
        "dataset": "test_dataset_eval",
    }

    # delete evaluation
    response = await client.delete(
        "/v0/evaluation",
        params=params,
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert response.json() == {
        "info": "Evaluation deleted successfully. You deleted 6 evaluations."
    }

    # check deleted
    expected_scores = {}
    all_params = {"dataset": "test_dataset_eval"}
    await _helper_test_list_evaluations(client, all_params, expected_scores)


async def test_delete_bad_evaluator(client: AsyncClient, dbsession):

    await _seed_evaluations_db(dbsession)

    params = {"dataset": "test_dataset_eval", "evaluator": "fake_evaluator"}

    # delete evaluation
    response = await client.delete(
        "/v0/evaluation",
        params=params,
        headers=HEADERS,
    )
    assert response.status_code == 404
    assert response.json() == {
        "detail": "The evaluator fake_evaluator does not exist in your account"
    }


async def test_delete_bad_endpoint(client: AsyncClient, dbsession):

    await _seed_evaluations_db(dbsession)

    params = {"dataset": "test_dataset_eval", "endpoint": "llama-5b@amazon.com"}

    # delete evaluation
    response = await client.delete(
        "/v0/evaluation",
        params=params,
        headers=HEADERS,
    )
    assert response.status_code == 404
    assert response.json() == {"detail": "Could not find endpoint: llama-5b@amazon.com"}
