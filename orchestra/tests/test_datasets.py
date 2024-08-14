import copy
import os

import pytest
from httpx import AsyncClient

from orchestra.tests.utils import HEADERS
from orchestra.web.api.dataset.views import bucket_name
from orchestra.web.api.utils.gcp import (
    blob_exists,
    delete_dir,
    dir_exists,
    internal_id_to_displayname,
)

user_id = os.getenv("AUTH_ACCOUNT_USER_ID")
headers = copy.copy(HEADERS)
headers.pop("Content-Type", None)


@pytest.fixture
def cleanup():
    to_remove = []
    yield to_remove
    lut = internal_id_to_displayname(user_id)
    lut = {name: id_ for id_, name in lut.items()}
    for name in to_remove:
        internal_id = lut.get(name, name)
        dir_name = f"{user_id}/{internal_id}/"
        if dir_exists(bucket_name, dir_name):
            delete_dir(bucket_name, dir_name)


def assert_correct_upload(response, name):
    assert response.status_code == 200
    assert response.json()["info"] == "Dataset uploaded succesfully!"
    lut = internal_id_to_displayname(user_id)
    lut = {name: id_ for id_, name in lut.items()}
    blob_name = f"{user_id}/{lut.get(name, name)}/0/dataset.jsonl"
    assert blob_exists("uploaded_datasets", blob_name)


def assert_delete(response, name):
    assert response.status_code == 200
    assert response.json()["info"] == "Dataset deleted succesfully!"
    lut = internal_id_to_displayname(user_id)
    lut = {name: id_ for id_, name in lut.items()}
    dir_name = f"{user_id}/{lut.get(name, name)}/"
    assert not dir_exists("uploaded_datasets", dir_name)


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


async def test_upload_dataset_invalid_name(client: AsyncClient, cleanup):

    file_path = "./orchestra/tests/sample_datasets/prompts.jsonl"
    name = "../dataset"
    cleanup.append(name)

    # Upload dataset
    response = await upload_dataset(client, file_path, name)
    assert response.status_code == 400
    assert (
        response.json()["detail"]
        == "Invalid name for a dataset. Please, choose a different one."
    )


async def test_upload_dataset(client: AsyncClient, cleanup):

    file_path = "./orchestra/tests/sample_datasets/prompts.jsonl"
    name = "test_upload_dataset"
    cleanup.append(name)

    # Upload dataset
    response = await upload_dataset(client, file_path, name)
    assert_correct_upload(response, name)

    # Already uploaded dataset
    response = await upload_dataset(client, file_path, name)
    assert response.status_code == 400
    assert (
        response.json()["detail"]
        == "A dataset with this name already exists. Please, choose a different one."
    )

    # Clean-up
    response = await delete_dataset(client, name)
    assert_delete(response, name)


async def test_upload_incorrect_dataset(client: AsyncClient, cleanup):

    names = ["wrong", "extra_kw", "no_prompt"]
    details = [
        (
            "The uploaded dataset has the wrong format. It must be a jsonl file where"
            " each line has a `prompt` key and optionally a `ref_answer` one."
        ),
        (
            "The uploaded dataset has the wrong format. It must be a jsonl file where"
            " each line has a `prompt` key and optionally a `ref_answer` one."
            " Unknown keyword `expected_answer` in line 2."
        ),
        (
            "The uploaded dataset has the wrong format. It must be a jsonl file where"
            " each line has a `prompt` key and optionally a `ref_answer` one."
            " Key `prompt` not found in line 1."
        ),
    ]

    for name, detail in zip(names, details):

        file_path = f"./orchestra/tests/sample_datasets/{name}.jsonl"
        cleanup.append(name)

        # Upload dataset
        response = await upload_dataset(client, file_path, name)
        assert response.status_code == 400
        assert response.json()["detail"] == detail


async def test_list_datasets(client: AsyncClient, cleanup):

    file_path = "./orchestra/tests/sample_datasets/prompts.jsonl"
    names = [f"test_list_dataset_{i}" for i in range(3)]
    cleanup += names

    for name in names:
        # Upload datasets
        response = await upload_dataset(client, file_path, name)
        assert_correct_upload(response, name)

    # List datasets
    response = await client.get("/v0/dataset/list", headers=headers)
    # checks
    datasets = response.json()
    # No full paths
    assert not any(["/" in d for d in datasets])
    # The datasets are contained in the list
    assert set(names) <= set(datasets)
    # No repeated elements
    assert len(datasets) == len(set(datasets))

    # Clean-up
    for name in names:
        response = await delete_dataset(client, name)
        assert_delete(response, name)


async def test_download_datasets(client: AsyncClient, cleanup):

    file_path = "./orchestra/tests/sample_datasets/prompts.jsonl"
    name = "test_download_dataset"
    cleanup.append(name)

    # Upload dataset
    response = await upload_dataset(client, file_path, name)
    assert_correct_upload(response, name)

    # Download dataset
    params = {"name": name}
    response = await client.get("/v0/dataset", headers=headers, params=params)
    jsonl = response.json()
    assert jsonl[0]["prompt"] == "This is the first prompt"
    assert len(jsonl) == 3

    # Clean-up
    response = await delete_dataset(client, name)
    assert_delete(response, name)


async def test_prompt_history(client: AsyncClient):
    url = "/v0/prompt_history"
    params = {"tag": None}
    response = await client.get(url, params=params, headers=headers)
    assert response.status_code == 200, response.json()
    assert isinstance(response.json(), list) and len(response.json()) == 0
    payload = {
        "model": "llama-3-8b-chat@aws-bedrock",
        "messages": [{"role": "user", "content": "Say hello."}],
    }
    response = await client.post("v0/chat/completions", json=payload, headers=headers)
    assert response.status_code == 200, response.json()
    url = "/v0/prompt_history"
    params = {"tag": None}
    response = await client.get(url, params=params, headers=headers)
    assert response.status_code == 200, response.json()
    assert isinstance(response.json(), list) and len(response.json()) > 0
