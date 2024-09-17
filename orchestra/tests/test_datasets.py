import copy
import json
import os

import pytest
from httpx import AsyncClient
from sqlalchemy import text

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


def _helper_check_downloads_match(expected, actual):
    for exp, act in zip(expected, actual):
        exp.pop("timestamp", None)
        act.pop("timestamp", None)
        assert exp == act


@pytest.mark.anyio
async def test_download_dataset_prompts_only(client: AsyncClient):

    file_path = "./orchestra/tests/sample_datasets/prompts_only.jsonl"
    name = "test_download_dataset"

    # Upload dataset
    response = await upload_dataset(client, file_path, name)
    assert response.status_code == 200, response.json()

    # Download dataset
    params = {"name": name}
    response = await client.get("/v0/dataset", headers=headers, params=params)
    jsonl = response.json()

    expected = [
        {
            "id": 1,
            "num_tokens": 0,
            "prompt": {
                "messages": [
                    {"role": "user", "content": "What is the capital of Spain?"}
                ]
            },
        },
        {
            "id": 2,
            "num_tokens": 0,
            "prompt": {
                "messages": [
                    {
                        "role": "user",
                        "content": "What is the square root of 1009 to 1 decimal place",
                    }
                ]
            },
        },
    ]

    _helper_check_downloads_match(expected, jsonl)


@pytest.mark.anyio
async def test_download_dataset_prompts_with_keys(client: AsyncClient):

    file_path = "./orchestra/tests/sample_datasets/prompts_with_kws.jsonl"
    name = "test_download_dataset"

    # Upload dataset
    response = await upload_dataset(client, file_path, name)
    assert response.status_code == 200, response.json()

    # Download dataset
    params = {"name": name}
    response = await client.get("/v0/dataset", headers=headers, params=params)
    jsonl = response.json()
    expected = [
        {
            "id": 1,
            "ref_answer": "Madrid",
            "num_tokens": 0,
            "topic": "Geography",
            "difficulty": "Easy",
            "prompt": {
                "messages": [
                    {"role": "user", "content": "What is the capital of Spain?"}
                ]
            },
        },
        {
            "id": 2,
            "ref_answer": "31.8",
            "num_tokens": 0,
            "topic": "Maths",
            "prompt": {
                "messages": [
                    {
                        "role": "user",
                        "content": "What is the square root of 1009 to 1 decimal place",
                    }
                ]
            },
        },
    ]

    _helper_check_downloads_match(expected, jsonl)


async def populate_from_file(path, session):
    with open(path) as f:
        for line in f:
            command = json.loads(line)
            statement = command["statement"]
            statement = statement.replace("%(", ":").replace(")s", "")
            session.execute(text(statement), command["parameters"])


async def _seed_datasets_db(dbsession):
    path = "./orchestra/tests/sql_dumps/datasets/dataset_dump.jsonl"
    await populate_from_file(path=path, session=dbsession)


async def _download_dataset(client, name):
    params = {"name": name}
    response = await client.get("/v0/dataset", headers=headers, params=params)
    jsonl = response.json()
    return jsonl


@pytest.mark.anyio
async def test_atomic_prompt_add_single(client: AsyncClient, dbsession):
    await _seed_datasets_db(dbsession)
    name = "test_upload_dataset"

    new_prompt = {
        "messages": [
            {"role": "user", "content": "What is the powerhouse of the cell?"},
        ],
    }
    data = {"name": name, "data": {"prompt": new_prompt}}
    response = await client.post("/v0/dataset/data", headers=headers, json=data)
    assert response.status_code == 200, response.json()

    # Download dataset
    jsonl = await _download_dataset(client, name)
    expected = [
        {
            "id": 1,
            "ref_answer": "Madrid",
            "num_tokens": 0,
            "topic": "Geography",
            "difficulty": "Easy",
            "prompt": {
                "messages": [
                    {"role": "user", "content": "What is the capital of Spain?"}
                ]
            },
        },
        {
            "id": 2,
            "ref_answer": "31.8",
            "num_tokens": 0,
            "topic": "Maths",
            "prompt": {
                "messages": [
                    {
                        "role": "user",
                        "content": "What is the square root of 1009 to 1 decimal place",
                    }
                ]
            },
        },
        {
            "id": 8,
            "num_tokens": 0,
            "prompt": {
                "messages": [
                    {"role": "user", "content": "What is the powerhouse of the cell?"}
                ]
            },
        },
    ]
    _helper_check_downloads_match(expected, jsonl)


@pytest.mark.anyio
async def test_atomic_prompt_add_multiple(client: AsyncClient, dbsession):
    await _seed_datasets_db(dbsession)
    name = "test_upload_dataset"

    new_prompts = [
        {
            "prompt": {
                "messages": [
                    {"role": "user", "content": "What is the powerhouse of the cell?"},
                ],
            }
        },
        {
            "prompt": {
                "messages": [
                    {"role": "user", "content": "What is the longest river in Europe?"},
                ],
            }
        },
    ]
    data = {"name": name, "data": new_prompts}
    response = await client.post("/v0/dataset/data", headers=headers, json=data)
    assert response.status_code == 200, response.json()

    # Download dataset
    jsonl = await _download_dataset(client, name)
    expected = [
        {
            "id": 1,
            "ref_answer": "Madrid",
            "num_tokens": 0,
            "topic": "Geography",
            "difficulty": "Easy",
            "prompt": {
                "messages": [
                    {"role": "user", "content": "What is the capital of Spain?"}
                ]
            },
        },
        {
            "id": 2,
            "ref_answer": "31.8",
            "num_tokens": 0,
            "topic": "Maths",
            "prompt": {
                "messages": [
                    {
                        "role": "user",
                        "content": "What is the square root of 1009 to 1 decimal place",
                    }
                ]
            },
        },
        {
            "id": 8,
            "num_tokens": 0,
            "prompt": {
                "messages": [
                    {"role": "user", "content": "What is the powerhouse of the cell?"}
                ]
            },
        },
        {
            "id": 9,
            "num_tokens": 0,
            "prompt": {
                "messages": [
                    {"role": "user", "content": "What is the longest river in Europe?"}
                ]
            },
        },
    ]
    _helper_check_downloads_match(expected, jsonl)


@pytest.mark.anyio
async def test_atomic_prompt_delete_single(client: AsyncClient, dbsession):
    await _seed_datasets_db(dbsession)
    name = "test_upload_dataset"
    _id = 1

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
    expected = [
        {
            "id": 2,
            "ref_answer": "31.8",
            "num_tokens": 0,
            "topic": "Maths",
            "prompt": {
                "messages": [
                    {
                        "role": "user",
                        "content": "What is the square root of 1009 to 1 decimal place",
                    }
                ]
            },
        }
    ]
    _helper_check_downloads_match(expected, jsonl)


@pytest.mark.anyio
async def test_atomic_prompt_delete_multiple(client: AsyncClient, dbsession):
    await _seed_datasets_db(dbsession)
    name = "test_second_upload_dataset"
    _ids = [4, 5, 6, 7]

    data = {"name": name, "data_ids": _ids}
    response = await client.delete(
        "/v0/dataset/data",
        headers=headers,
        params=data,
    )
    assert response.status_code == 200, response.json()

    params = {"name": name}
    response = await client.get("/v0/dataset", headers=headers, params=params)
    jsonl = response.json()
    expected = [
        {
            "id": 3,
            "ref_answer": "William Shakespeare",
            "num_tokens": 0,
            "topic": "Literature",
            "prompt": {
                "messages": [
                    {
                        "role": "user",
                        "content": "Who wrote the play 'Romeo and Juliet'?",
                    }
                ]
            },
        }
    ]
    _helper_check_downloads_match(expected, jsonl)


@pytest.mark.anyio
async def test_atomic_prompt_add_duplicate(client: AsyncClient, dbsession):
    await _seed_datasets_db(dbsession)
    name = "test_upload_dataset"

    new_prompt = {
        "ref_answer": "Madrid",
        "topic": "Geography",
        "difficulty": "Easy",
        "prompt": {
            "messages": [{"role": "user", "content": "What is the capital of Spain?"}]
        },
    }
    data = {"name": name, "data": new_prompt}
    response = await client.post("/v0/dataset/data", headers=headers, json=data)
    assert response.status_code == 404, response.json()
    assert response.json() == "error"



@pytest.mark.anyio
async def test_atomic_prompt_duplicate_add_ignored(client: AsyncClient):
    file_path = "./orchestra/tests/sample_datasets/new_prompts.jsonl"
    name = "test_duplicate_prompt_ignored"
    response = await upload_dataset(client, file_path, name)
    assert response.status_code == 200, response.json()

    with open(file_path) as file:
        duplicate_data = file.read()
    duplicate_data = json.loads(duplicate_data.split("\n")[0])

    data = {"name": name, "data": duplicate_data}
    response = await client.post("/v0/dataset/data", headers=headers, json=data)
    assert response.status_code == 200, response.json()

    dataset = await client.get("/v0/dataset", headers=headers, params={"name": name})
    dataset = json.loads(dataset.text)
    assert len(dataset) == 2


@pytest.mark.anyio
async def test_dataset_extra_fields_added(client: AsyncClient):
    file_path = "./orchestra/tests/sample_datasets/new_prompts.jsonl"
    name = "test_extra_fields_stored"
    response = await upload_dataset(client, file_path, name)
    assert response.status_code == 200, response.json()

    dataset = await client.get("/v0/dataset", headers=headers, params={"name": name})
    dataset = json.loads(dataset.text)
    assert "ref_answer" in dataset[0]
