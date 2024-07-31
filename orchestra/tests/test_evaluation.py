import asyncio
import requests
import os
import time
import json

import pytest
from httpx import AsyncClient

api_key = str(os.getenv("AUTH_ACCOUNT_API_KEY"))

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


# tests /evaluation
@pytest.mark.anyio
async def test_evaluation(client: AsyncClient):
    # upload dataset
    dataset_name = f"test_dataset_EVALUATION_{int(time.time()*1000 % 100000)}"
    ret = await _upload_dataset(client, dataset_name, sample_path)
    assert ret.status_code == 200
    await asyncio.sleep(5)

    # evaluate dataset
    endpoint = "llama-3-8b-chat@aws-bedrock"
    judge_models = ["claude-3-haiku@aws-bedrock"]
    params = {
        "dataset": dataset_name,
        "endpoint": endpoint,
        "judge_models": judge_models,
    }
    response = await client.post("/v0/evaluation", json=params, headers=HEADERS)
    assert response.status_code == 200
    await asyncio.sleep(30)

    # check evaluation in list
    response = await client.get("/v0/evaluation/list", headers=HEADERS)
    assert dataset_name in response.json()

    # check evaluation in results
    response = await client.get(
        f"/v0/evaluation/results?dataset={dataset_name}", headers=HEADERS
    )
    assert response.status_code == 200

    # cleanup
    # TODO: move this to a fixture
    ret = await _delete_dataset_evaluation(client, dataset_name)
    assert ret.status_code == 200


# tests DELETE /evaluation
@pytest.mark.anyio
async def test_evaluation_delete(client: AsyncClient):
    # upload dataset
    dataset_name = f"test_dataset_DELETE_{int(time.time()*1000 % 100000)}"
    ret = await _upload_dataset(client, dataset_name, sample_path)
    assert ret.status_code == 200
    await asyncio.sleep(5)
    # trigger evaluation
    endpoint = "llama-3-8b-chat@aws-bedrock"
    judge_models = ["claude-3-haiku@aws-bedrock"]
    params = {
        "dataset": dataset_name,
        "endpoint": endpoint,
        "judge_models": judge_models,
    }
    response = await client.post("/v0/evaluation", json=params, headers=HEADERS)
    assert response.status_code == 200
    await asyncio.sleep(30)

    # check in list
    response = await client.get("/v0/evaluation/list", headers=HEADERS)
    assert dataset_name in response.json()

    # check in results
    response = await client.get(
        f"/v0/evaluation/results?dataset={dataset_name}", headers=HEADERS
    )
    assert response.status_code == 200

    # delete evaluation
    ret = await _delete_dataset_evaluation(client, dataset_name)
    assert ret.status_code == 200

    await asyncio.sleep(5)

    # check not in list
    response = await client.get("/v0/evaluation/list", headers=HEADERS)
    assert dataset_name not in response.json()

    # check not in results
    response = await client.get(
        "/v0/evaluation/results?dataset={dataset_name}", headers=HEADERS
    )
    assert response.status_code != 200


# /evaluation/list and /evaluation/results
# are tested implicitly in evaluate & delete
