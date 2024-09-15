import copy
import os

import pytest
from httpx import AsyncClient

from orchestra.tests.utils import HEADERS

user_id = os.getenv("AUTH_ACCOUNT_USER_ID")
headers = copy.copy(HEADERS)
headers.pop("Content-Type", None)


def upload_dataset(client, file_path, name):

    with open(file_path, "rb") as f:
        file_content = f.read()
    # Prepare the multipart form data
    files = {"file": ("test.jsonl", file_content, "application/x-jsonlines")}
    data = {"name": name}
    # Send POST request to the /dataset endpoint
    return client.post("/v0/dataset", headers=headers, data=data, files=files)


def delete_dataset(client, name):
    params = {"name": name}
    # Send DELETE request to the /dataset endpoint
    return client.delete("/v0/dataset", headers=headers, params=params)


def fetch_datasets(client):
    url = "/v0/dataset/list"
    return client.get(url, headers=headers)


@pytest.mark.anyio
async def test_upload_dataset(client: AsyncClient):
    # Upload dataset
    file_path = "./orchestra/tests/sample_datasets/new_prompts.jsonl"
    name = "test_upload_dataset"
    response = await upload_dataset(client, file_path, name)
    assert response.status_code == 200, response.json()

    response = await fetch_datasets(client)
    assert name in response.json()


@pytest.mark.anyio
async def test_upload_duplicate_dataset(client: AsyncClient):
    file_path = "./orchestra/tests/sample_datasets/new_prompts.jsonl"
    name = "test_upload_dataset"
    response = await upload_dataset(client, file_path, name)
    response = await upload_dataset(client, file_path, name)
    assert response.status_code == 400


@pytest.mark.anyio
async def test_upload_incorrect_dataset(client: AsyncClient):

    names = ["wrong", "no_prompt"]
    # TODO: make the endpoint error messages reflect these details
    details = [
        (
            "The uploaded dataset has the wrong format. It must be a jsonl file where"
            " each line has a `prompt` key and optionally a `ref_answer` one."
        ),
        (
            "The uploaded dataset has the wrong format. It must be a jsonl file where"
            " each line has a `prompt` key and optionally a `ref_answer` one."
            " Key `prompt` not found in line 1."
        ),
    ]

    for name, detail in zip(names, details):

        file_path = f"./orchestra/tests/sample_datasets/{name}.jsonl"

        # Upload dataset
        response = await upload_dataset(client, file_path, name)
        assert response.status_code == 400


@pytest.mark.anyio
async def test_rename_dataset(client: AsyncClient):
    file_path = "./orchestra/tests/sample_datasets/new_prompts.jsonl"
    name = "test_old_name"
    response = await upload_dataset(client, file_path, name)
    assert response.status_code == 200, response.json()
    new_name = "test_new_name"
    params = {"name": name, "new_name": new_name}
    response = await client.post("/v0/dataset/rename", headers=headers, params=params)
    response = await fetch_datasets(client)
    assert new_name in response.json()
    assert name not in response.json()


@pytest.mark.anyio
async def test_list_datasets(client: AsyncClient):

    file_path = "./orchestra/tests/sample_datasets/new_prompts.jsonl"
    names = [f"test_list_dataset_{i}" for i in range(3)]

    for name in names:
        # Upload datasets
        response = await upload_dataset(client, file_path, name)
        assert response.status_code == 200, response.json()

    # List datasets
    response = await fetch_datasets(client)
    # checks
    datasets = response.json()
    # No full paths
    assert not any(["/" in d for d in datasets])
    # The datasets are contained in the list
    assert set(names) <= set(datasets)
    # No repeated elements
    assert len(datasets) == len(set(datasets))


@pytest.mark.anyio
async def test_download_datasets(client: AsyncClient):

    file_path = "./orchestra/tests/sample_datasets/new_prompts.jsonl"
    name = "test_download_dataset"

    # Upload dataset
    response = await upload_dataset(client, file_path, name)
    assert response.status_code == 200, response.json()

    # Download dataset
    params = {"name": name}
    response = await client.get("/v0/dataset", headers=headers, params=params)
    jsonl = response.json()
    assert (
        jsonl[0]["prompt"]["messages"][0]["content"] == "What is the capital of Spain?"
    )
    assert len(jsonl) == 2


@pytest.mark.anyio
async def test_atomic_prompt_fns(client: AsyncClient):
    file_path = "./orchestra/tests/sample_datasets/new_prompts.jsonl"
    name = "test_add_one_prompt"
    response = await upload_dataset(client, file_path, name)
    assert response.status_code == 200, response.json()

    new_prompt = {
        "messages": [
            {"role": "user", "content": "What is the powerhouse of the cell?"},
        ],
    }
    data = {"name": name, "data": {"prompt": new_prompt}}
    response = await client.post("/v0/dataset/data", headers=headers, json=data)
    assert response.status_code == 200, response.json()

    # Download dataset
    params = {"name": name}
    response = await client.get("/v0/dataset", headers=headers, params=params)
    jsonl = response.json()
    assert len(jsonl) == 3

    _id = jsonl[0]["id"]

    data = {"name": name, "data_ids": _id}
    response = await client.delete(
        "/v0/dataset/data",
        headers=headers,
        params=data,
    )
    assert response.status_code == 200, response.json()

    params = {"name": name}
    response = await client.get("/v0/dataset", headers=headers, params=params)
    jsonl = response.json()
    assert len(jsonl) == 2
    assert jsonl[0]["prompt"]["messages"] == [
        {
            "role": "user",
            "content": "What is the square root of 1009 to 1 decimal place",
        },
    ]
