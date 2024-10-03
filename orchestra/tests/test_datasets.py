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


# helpers


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


def _helper_check_downloads_match(expected, actual):
    for exp, act in zip(expected, actual):
        exp.pop("timestamp", None)
        act.pop("timestamp", None)
        assert exp == act


### prompts for testing

madrid_prompt = {
    "ref_answer": "Madrid",
    "topic": "Geography",
    "difficulty": "Easy",
    "prompt": {
        "messages": [{"role": "user", "content": "What is the capital of Spain?"}]
    },
}
madrid_prompt_with_id = {"id": 1, "num_tokens": 7, **madrid_prompt}

squareroot_prompt = {
    "ref_answer": "31.8",
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
squareroot_prompt_with_id = {"id": 2, "num_tokens": 14, **squareroot_prompt}

mitochondria_prompt = {
    "prompt": {
        "messages": [{"role": "user", "content": "What is the powerhouse of the cell?"}]
    },
    "topic": "Biology",
}
mitochondria_prompt_with_id = {"id": 8, "num_tokens": 8, **mitochondria_prompt}

river_prompt = {
    "prompt": {
        "messages": [
            {"role": "user", "content": "What is the longest river in Europe?"},
        ],
    }
}
river_prompt_with_id = {"id": 9, "num_tokens": 8, **river_prompt}


shakespeare_prompt = {
    "ref_answer": "William Shakespeare",
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
shakespeare_prompt_with_id = {"id": 3, "num_tokens": 11, **shakespeare_prompt}


# tests


@pytest.mark.anyio
async def test_upload_dataset(client: AsyncClient):
    # Upload dataset
    file_path = "./orchestra/tests/sample_datasets/prompts_with_kws.jsonl"
    name = "test_upload_dataset"
    response = await upload_dataset(client, file_path, name)
    assert response.status_code == 200, response.json()

    response = await fetch_datasets(client)
    assert name in response.json()


@pytest.mark.anyio
async def test_upload_duplicate_dataset(client: AsyncClient):
    file_path = "./orchestra/tests/sample_datasets/prompts_with_kws.jsonl"
    name = "test_upload_dataset"
    response = await upload_dataset(client, file_path, name)
    response = await upload_dataset(client, file_path, name)
    assert response.status_code == 400
    assert response.json() == {"detail": f"Dataset {name} already exists."}


@pytest.mark.anyio
async def test_upload_incorrect_dataset(client: AsyncClient):

    names = ["wrong", "no_prompt"]
    # TODO: make the endpoint error messages reflect these details
    details = [
        (
            "The uploaded dataset has the wrong format. It must be a jsonl file where"
            " each line has a `prompt` key."
        ),
        (
            "The uploaded dataset has the wrong format. It must be a jsonl file where"
            " each line has a `prompt` key."
            " Key `prompt` not found in line 1."
        ),
    ]

    for name, detail in zip(names, details):

        file_path = f"./orchestra/tests/sample_datasets/{name}.jsonl"

        # Upload dataset
        response = await upload_dataset(client, file_path, name)
        assert response.status_code == 400
        assert response.json() == {"detail": detail}


@pytest.mark.anyio
async def test_rename_dataset(client: AsyncClient):
    file_path = "./orchestra/tests/sample_datasets/prompts_with_kws.jsonl"
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

    file_path = "./orchestra/tests/sample_datasets/prompts_with_kws.jsonl"
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
async def test_download_dataset_prompts_only(client: AsyncClient):

    file_path = "./orchestra/tests/sample_datasets/prompts_only.jsonl"
    name = "test_download_dataset"

    # Upload dataset
    response = await upload_dataset(client, file_path, name)
    assert response.status_code == 200, response.json()

    # Download dataset
    params = {"name": name}
    response = await client.get("/v0/dataset", headers=headers, params=params)
    assert response.status_code == 200, response.json()

    jsonl = response.json()

    expected = [
        {
            "id": 1,
            "num_tokens": 7,
            "prompt": {
                "messages": [
                    {"role": "user", "content": "What is the capital of Spain?"}
                ]
            },
        },
        {
            "id": 2,
            "num_tokens": 14,
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
        madrid_prompt_with_id,
        squareroot_prompt_with_id,
    ]

    _helper_check_downloads_match(expected, jsonl)


@pytest.mark.anyio
async def test_rename_dataset_from_seed(client: AsyncClient, dbsession):
    await _seed_datasets_db(dbsession)
    name = "test_upload_dataset"
    new_name = "test_new_name"
    params = {"name": name, "new_name": new_name}
    response = await client.post("/v0/dataset/rename", headers=headers, params=params)
    assert response.status_code == 200, response.json()
    assert response.json() == {"info": "Dataset name updated successfully!"}
    response = await fetch_datasets(client)
    assert response.status_code == 200, response.json()
    assert response.json() == ["test_new_name", "test_second_upload_dataset"]


async def test_rename_dataset_invalid_oldname(client: AsyncClient, dbsession):
    await _seed_datasets_db(dbsession)
    name = "test_upload_dataset_fake"
    new_name = "test_new_name"
    params = {"name": name, "new_name": new_name}
    response = await client.post("/v0/dataset/rename", headers=headers, params=params)
    assert response.status_code == 400, response.json()
    assert response.json() == {"detail": f"You don't have a dataset named {name}"}


async def test_rename_dataset_invalid_newname(client: AsyncClient, dbsession):
    await _seed_datasets_db(dbsession)
    name = "test_upload_dataset"
    new_name = "test_second_upload_dataset"
    params = {"name": name, "new_name": new_name}
    response = await client.post("/v0/dataset/rename", headers=headers, params=params)
    assert response.status_code == 400, response.json()
    assert response.json() == {
        "detail": f"You already have a dataset named {new_name}."
    }


@pytest.mark.anyio
async def test_atomic_prompt_add_single(client: AsyncClient, dbsession):
    await _seed_datasets_db(dbsession)
    name = "test_upload_dataset"

    new_prompt = mitochondria_prompt
    data = {"name": name, "data": new_prompt}
    response = await client.post("/v0/dataset/data", headers=headers, json=data)
    assert response.status_code == 200, response.json()
    assert response.json() == {"info": "Data added successfully"}

    actual = await _download_dataset(client, name)
    expected = [
        madrid_prompt_with_id,
        squareroot_prompt_with_id,
        mitochondria_prompt_with_id,
    ]
    _helper_check_downloads_match(expected, actual)


@pytest.mark.anyio
async def test_atomic_prompt_add_multiple(client: AsyncClient, dbsession):
    await _seed_datasets_db(dbsession)
    name = "test_upload_dataset"

    new_prompts = [
        mitochondria_prompt,
        river_prompt,
    ]
    data = {"name": name, "data": new_prompts}
    response = await client.post("/v0/dataset/data", headers=headers, json=data)
    assert response.status_code == 200, response.json()

    actual = await _download_dataset(client, name)
    expected = [
        madrid_prompt_with_id,
        squareroot_prompt_with_id,
        mitochondria_prompt_with_id,
        river_prompt_with_id,
    ]
    _helper_check_downloads_match(expected, actual)


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

    actual = await _download_dataset(client, name)
    expected = [
        squareroot_prompt_with_id,
    ]
    _helper_check_downloads_match(expected, actual)


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

    actual = await _download_dataset(client, name)
    expected = [
        shakespeare_prompt_with_id,
    ]
    _helper_check_downloads_match(expected, actual)


async def test_atomic_prompt_add_duplicate(client: AsyncClient, dbsession):
    await _seed_datasets_db(dbsession)
    name = "test_upload_dataset"

    new_prompt = madrid_prompt
    data = {"name": name, "data": new_prompt, "ignore_duplicates": False}
    response = await client.post("/v0/dataset/data", headers=headers, json=data)
    assert response.status_code == 400, response.json()
    assert response.json() == {
        "detail": "There was an error adding the prompt.\nErrors:\nError with prompt 1: This prompt is already in the dataset\n"
    }

    actual = await _download_dataset(client, name)
    expected = [
        madrid_prompt_with_id,
        squareroot_prompt_with_id,
    ]
    _helper_check_downloads_match(expected, actual)


async def test_atomic_prompt_add_from_other_dataset(client: AsyncClient, dbsession):
    await _seed_datasets_db(dbsession)

    name = "test_upload_dataset"

    new_prompt = shakespeare_prompt
    data = {"name": name, "data": new_prompt}
    response = await client.post("/v0/dataset/data", headers=headers, json=data)
    assert response.status_code == 200, response.json()

    actual = await _download_dataset(client, name)
    expected = [
        madrid_prompt_with_id,
        squareroot_prompt_with_id,
        shakespeare_prompt_with_id,
    ]
    _helper_check_downloads_match(expected, actual)


async def test_atomic_prompt_add_multiple_with_some_duplicates(
    client: AsyncClient, dbsession
):
    await _seed_datasets_db(dbsession)
    name = "test_upload_dataset"

    new_prompts = [
        mitochondria_prompt,
        river_prompt,
        madrid_prompt,
        squareroot_prompt,
    ]
    data = {"name": name, "data": new_prompts, "ignore_duplicates": False}
    response = await client.post("/v0/dataset/data", headers=headers, json=data)
    assert response.status_code == 400, response.json()
    assert response.json() == {
        "detail": "There was an error while adding some of the prompts.\nThere were 2 prompts added successfuly, and 2 errors.\nErrors:\nError with prompt 3: This prompt is already in the dataset\nError with prompt 4: This prompt is already in the dataset\n"
    }
    actual = await _download_dataset(client, name)
    expected = [
        madrid_prompt_with_id,
        squareroot_prompt_with_id,
        mitochondria_prompt_with_id,
        river_prompt_with_id,
    ]
    _helper_check_downloads_match(expected, actual)


@pytest.mark.anyio
async def test_dataset_extra_fields_added(client: AsyncClient):
    file_path = "./orchestra/tests/sample_datasets/prompts_with_kws.jsonl"
    name = "test_extra_fields_stored"
    response = await upload_dataset(client, file_path, name)
    assert response.status_code == 200, response.json()

    dataset = await client.get("/v0/dataset", headers=headers, params={"name": name})
    dataset = dataset.json()
    assert "topic" in dataset[0]


@pytest.mark.xfail
@pytest.mark.anyio
async def test_extra_fields_repeated(client: AsyncClient, dbsession):
    await _seed_datasets_db(dbsession)
    name = "test_upload_dataset"

    new_prompt = mitochondria_prompt
    data = {"name": name, "data": new_prompt}
    response = await client.post("/v0/dataset/data", headers=headers, json=data)
    assert response.status_code == 200, response.json()
    assert response.json() == {"info": "Data added successfully"}

    # add again, with a new extra_field
    new_prompt_2 = mitochondria_prompt.copy()
    new_prompt_2["topic"] = "biochemistry"
    new_prompt_2["difficulty"] = "medium"

    data = {"name": name, "data": new_prompt_2}
    response = await client.post("/v0/dataset/data", headers=headers, json=data)
    assert response.status_code == 200, response.json()
    assert response.json() == {"info": "Data added successfully"}
    prompt = actual[3]

    new_mitochondria_prompt_with_id = {"id": 8, "num_tokens": 8, **new_prompt_2}

    actual = await _download_dataset(client, name)
    expected = [
        madrid_prompt_with_id,
        squareroot_prompt_with_id,
        new_mitochondria_prompt_with_id,
    ]
    _helper_check_downloads_match(expected, actual)


async def test_add_prompt_invalid_pydantic(client: AsyncClient, dbsession):
    await _seed_datasets_db(dbsession)
    name = "test_upload_dataset"

    bad_prompt = {
        "prompt": {
            "messages": [
                {"role": "user", "content": "What is the powerhouse of the cell?"}
            ],
            "fake_kw": 123,
        },
    }
    data = {"name": name, "data": bad_prompt}
    response = await client.post("/v0/dataset/data", headers=headers, json=data)
    assert response.status_code == 400, response.json()
    assert response.json() == {
        "detail": "There was an error adding the prompt.\nErrors:\nError with prompt 1: 1 validation error for Prompt\nfake_kw\n  Extra inputs are not permitted [type=extra_forbidden, input_value=123, input_type=int]\n    For further information visit https://errors.pydantic.dev/2.9/v/extra_forbidden\n"
    }
