# CLEANUP: Delete this file
import json
import os

import pytest
from httpx import AsyncClient

api_key = str(os.getenv("AUTH_ACCOUNT_API_KEY"))

HEADERS = {
    "accept": "application/json",
    "Authorization": f"Bearer {api_key}",
}

sample_path = "./orchestra/tests/sample_datasets/with_ref.jsonl"


@pytest.mark.anyio
async def test_create_eval(
    client: AsyncClient,
):
    eval_name = "test_eval_config"
    system_prompt = "dummy system prompt {user_prompt} {response} {class_config}"

    url = "/v0/evaluator"
    params = {"name": eval_name, "judge_prompt": system_prompt}
    response = await client.post(url, json=params, headers=HEADERS)
    assert response.status_code == 200, response.json()

    url = "/v0/evaluator/list"
    response = await client.get(url, headers=HEADERS)
    assert eval_name in response.json()

    url = "/v0/evaluator"
    params = {"name": eval_name}
    response = await client.get(url, params=params, headers=HEADERS)
    assert (
        json.loads(response.json()["judge_prompt"])["messages"][-1]["content"]
        == system_prompt
    )


@pytest.mark.anyio
async def test_create_eval_duplicate(
    client: AsyncClient,
):
    eval_name = "test_eval_config"
    system_prompt = "dummy system prompt {user_prompt} {response} {class_config}"

    url = "/v0/evaluator"
    params = {"name": eval_name, "judge_prompt": system_prompt}

    response = await client.post(url, json=params, headers=HEADERS)
    assert response.status_code == 200, response.json()

    # make it a second time
    response = await client.post(url, json=params, headers=HEADERS)
    assert response.status_code == 404, response.json()


@pytest.mark.anyio
async def test_delete_eval(
    client: AsyncClient,
):
    eval_name = "test_eval_to_delete"
    system_prompt = "dummy system prompt"

    url = "/v0/evaluator"
    params = {"name": eval_name, "system_prompt": system_prompt}
    response = await client.post(url, json=params, headers=HEADERS)
    assert response.status_code == 200, response.json()

    url = "/v0/evaluator"
    params = {"name": eval_name}
    response = await client.delete(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()

    url = "/v0/evals/list_configs"
    response = await client.get(url, headers=HEADERS)
    assert eval_name not in response.json()


@pytest.mark.anyio
async def test_rename_eval(
    client: AsyncClient,
):
    eval_name = "test_eval_to_rename"
    system_prompt = "dummy system prompt"

    url = "/v0/evaluator"
    params = {"name": eval_name, "system_prompt": system_prompt}
    response = await client.post(url, json=params, headers=HEADERS)
    assert response.status_code == 200, response.json()

    url = "/v0/evaluator/rename"
    new_eval_name = "new_name_for_eval"
    params = {"name": eval_name, "new_name": new_eval_name}
    response = await client.post(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()

    url = "/v0/evaluator/list"
    response = await client.get(url, headers=HEADERS)
    assert eval_name not in response.json()
    assert new_eval_name in response.json()


@pytest.mark.anyio
async def test_invalid_judge_model(
    client: AsyncClient,
):
    eval_name = "invalid_judge_model"
    judge_model = "fake_judge123@fake_provider456"

    url = "/v0/evaluator"
    params = {"name": eval_name, "judge_models": judge_model}
    response = await client.post(url, json=params, headers=HEADERS)
    assert response.status_code == 400, response.json()
