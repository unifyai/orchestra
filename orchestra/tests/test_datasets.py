import copy
import os

from httpx import AsyncClient

from orchestra.tests.utils import HEADERS
from orchestra.web.api.dataset.views import blob_exists, dir_exists

user_id = os.getenv("AUTH_ACCOUNT_USER_ID")
headers = copy.copy(HEADERS)
headers.pop("Content-Type", None)


def assert_correct_upload(response, name):
    assert response.status_code == 200
    assert response.json()["info"] == "Dataset uploaded succesfully!"
    blob_name = f"{user_id}/{name}/0/dataset.jsonl"
    assert blob_exists("uploaded_datasets", blob_name)


def assert_delete(response, name):
    assert response.status_code == 200
    assert response.json()["info"] == "Dataset deleted succesfully!"
    dir_name = f"{user_id}/{name}/"
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


async def test_upload_dataset(client: AsyncClient):

    file_path = "./orchestra/tests/sample_datasets/prompts.jsonl"
    name = "test_upload_dataset"

    # Upload dataset
    response = await upload_dataset(client, file_path, name)
    assert_correct_upload(response, name)

    # Clean-up
    response = await delete_dataset(client, name)
    assert_delete(response, name)


async def test_list_datasets(client: AsyncClient):

    file_path = "./orchestra/tests/sample_datasets/prompts.jsonl"
    names = [f"test_upload_dataset_{i}" for i in range(3)]

    for name in names:
        # Upload datasets
        response = await upload_dataset(client, file_path, name)
        assert_correct_upload(response, name)

    # List datasets
    response = await client.get("/v0/dataset/list", headers=headers)
    assert set(response.json()) == set(names)

    # Clean-up
    for name in names:
        response = await delete_dataset(client, name)
        assert_delete(response, name)


# TODO: Test download
