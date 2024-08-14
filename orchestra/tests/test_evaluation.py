import asyncio
import json
import os
import requests
import subprocess
import sys
import time

import pytest
from httpx import AsyncClient
from google.cloud import storage

import orchestra
from orchestra.web.api.dataset_evaluation.views import build_displayname_to_id


# TODO: Less hacky way for this?
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, project_root)
dataset_eval_path = os.path.join(project_root, "dataset_evaluation")
sys.path.insert(0, dataset_eval_path)
from dataset_evaluation.evaluate_dataset import evaluate_dataset
from dataset_evaluation.refresh_scores import refresh_scores_for_user

api_key = str(os.getenv("AUTH_ACCOUNT_API_KEY"))
test_user_id = os.getenv("AUTH_ACCOUNT_USER_ID")

HEADERS = {
    "accept": "application/json",
    "Authorization": f"Bearer {api_key}",
}


def _upload_dataset(client, dataset_name, data_path):
    data = {"name": dataset_name}
    with open(data_path, "rb") as f:
        file_content = f.read()
    files = {"file": ("test.jsonl", file_content, "application/x-jsonlines")}
    response = client.post("/v0/dataset", data=data, files=files, headers=HEADERS)
    return response


def _delete_dataset_evaluation(client, dataset_name):
    response = client.delete(
        "/v0/dataset", params={"name": dataset_name}, headers=HEADERS
    )
    return response


sample_path = "./orchestra/tests/sample_datasets/with_ref.jsonl"


@pytest.fixture
def cleanup_eval_config():
    to_remove = []
    yield to_remove
    displayname_to_id = build_displayname_to_id(test_user_id)
    for eval_name in to_remove:
        if eval_name not in displayname_to_id:
            continue
        eval_id = displayname_to_id[eval_name]
        blob_name = f"{test_user_id}/evaluation_configs/{eval_id}.config"
        blob = storage.Client().bucket("uploaded_datasets").blob(blob_name)
        if blob.exists():
            blob.delete()


@pytest.mark.anyio
async def test_create_eval(
    client: AsyncClient,
    cleanup_eval_config,
):
    eval_name = "test_eval_config"
    system_prompt = "dummy system prompt"

    url = "/v0/evals/create"
    params = {"eval_name": eval_name, "system_prompt": system_prompt}
    cleanup_eval_config.append(eval_name)
    response = await client.post(url, json=params, headers=HEADERS)
    assert response.status_code == 200, response.json()

    url = "/v0/evals/list_configs"
    response = await client.get(url, headers=HEADERS)
    assert eval_name in response.json()

    url = "/v0/evals/get_config"
    params = {"eval_name": eval_name}
    response = await client.get(url, params=params, headers=HEADERS)
    assert response.json()["system_prompt"] == system_prompt


@pytest.mark.anyio
async def test_delete_eval(
    client: AsyncClient,
    cleanup_eval_config,
):
    eval_name = "test_eval_to_delete"
    system_prompt = "dummy system prompt"
    cleanup_eval_config.append(eval_name)

    url = "/v0/evals/create"
    params = {"eval_name": eval_name, "system_prompt": system_prompt}
    response = await client.post(url, json=params, headers=HEADERS)
    assert response.status_code == 200, response.json()

    url = "/v0/evals/delete"
    params = {"eval_name": eval_name}
    response = await client.delete(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()

    url = "/v0/evals/list_configs"
    response = await client.get(url, headers=HEADERS)
    assert eval_name not in response.json()


@pytest.mark.anyio
async def test_rename_eval(
    client: AsyncClient,
    cleanup_eval_config,
):
    eval_name = "test_eval_to_rename"
    system_prompt = "dummy system prompt"
    cleanup_eval_config.append(eval_name)

    url = "/v0/evals/create"
    params = {"eval_name": eval_name, "system_prompt": system_prompt}
    response = await client.post(url, json=params, headers=HEADERS)
    assert response.status_code == 200, response.json()

    url = "/v0/evals/rename"
    new_eval_name = "new_name_for_eval"
    cleanup_eval_config.append(new_eval_name)
    params = {"eval_name": eval_name, "new_eval_name": new_eval_name}
    response = await client.post(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()

    url = "/v0/evals/list_configs"
    response = await client.get(url, headers=HEADERS)
    assert eval_name not in response.json()
    assert new_eval_name in response.json()


async def test_trigger_eval(
    client: AsyncClient,
    cleanup_eval_config,
    tmp_path,
    monkeypatch,
):
    def mock_send_to_dataset_evaluation_server(action, **data):
        data.pop("user_email", "")
        message_data = json.dumps({"action": action, **data, "orchestra_url": ""})
        save_dir = tmp_path / "save_files"
        if action == "evaluate":
            asyncio.run(
                evaluate_dataset(
                    message_data, save_dir, shared_volume="", client=client
                )
            )
        elif action == "refresh_scores":
            user_id = data["user_id"]
            asyncio.run(refresh_scores_for_user(user_id, save_dir))
        else:
            raise NotImplementedError

    monkeypatch.setattr(
        orchestra.web.api.dataset_evaluation.views,
        "send_to_dataset_evaluation_server",
        mock_send_to_dataset_evaluation_server,
    )

    eval_name = "test_eval"
    system_prompt = "dummy system prompt"
    judge_model = "llama-3-8b-chat@aws-bedrock"
    cleanup_eval_config.append(eval_name)

    url = "/v0/evals/create"
    params = {
        "eval_name": eval_name,
        "system_prompt": system_prompt,
        "judge_models": judge_model,
    }
    response = await client.post(url, json=params, headers=HEADERS)
    assert response.status_code == 200, response.json()

    url = "/v0/evals/trigger"
    dataset = "test_dataset"
    endpoint = "llama-3-8b-chat@aws-bedrock"
    params = {
        "url": url,
        "dataset": dataset,
        "endpoint": endpoint,
        "eval_name": eval_name,
    }
    response = await client.post(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()

    url = "/v0/evals/get_scores"
    params = {"dataset": dataset, "eval_name": eval_name}
    response = await client.get(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()
    scores = response.json()
    assert eval_name in scores
    assert endpoint in scores[eval_name]
    assert judge_model in scores[eval_name][endpoint]

    # TODO: add cleanup_triggered_eval (this might need work in dataset_evaluation script)


# evals/get_scores is implicitly tested


async def test_client_side_scores(
    client: AsyncClient,
    cleanup_eval_config,
    tmp_path,
):
    eval_name = "test_eval_clientside"
    cleanup_eval_config.append(eval_name)

    url = "/v0/evals/create"
    params = {
        "eval_name": eval_name,
        "client_side": True,
    }
    response = await client.post(url, json=params, headers=HEADERS)
    assert response.status_code == 200, response.json()

    url = "/v0/evals/trigger"
    dataset = "test_dataset"
    endpoint = "llama-3-8b-chat@aws-bedrock"
    file_path = "./orchestra/tests/sample_datasets/prompts_with_scores.jsonl"
    with open(file_path, "rb") as f:
        file_content = f.read()
    files = {
        "client_side_scores": ("test.jsonl", file_content, "application/x-jsonlines")
    }

    params = {
        "url": url,
        "dataset": dataset,
        "endpoint": endpoint,
        "eval_name": eval_name,
    }
    response = await client.post(url, params=params, files=files, headers=HEADERS)
    assert response.status_code == 200, response.json()

    url = "/v0/evals/get_scores"
    params = {"dataset": dataset, "eval_name": eval_name}
    response = await client.get(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()
    scores = response.json()
    assert eval_name in scores
    assert endpoint in scores[eval_name]
    assert "client_side" in scores[eval_name][endpoint]
