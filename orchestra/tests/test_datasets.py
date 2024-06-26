import copy
import os

from httpx import AsyncClient

from orchestra.tests.utils import HEADERS
from orchestra.web.api.dataset.views import blob_exists, dir_exists


async def test_upload_dataset(client: AsyncClient):

    file_path = "./orchestra/tests/sample_datasets/prompts.jsonl"
    name = "test_upload_dataset"
    user_id = os.getenv("AUTH_ACCOUNT_USER_ID")
    blob_name = f"{user_id}/{name}/0/dataset.jsonl"

    headers = copy.copy(HEADERS)
    headers.pop("Content-Type", None)

    with open(file_path, "rb") as f:
        file_content = f.read()

    # Prepare the multipart form data
    files = {"file": ("test_dataset.jsonl", file_content, "application/x-jsonlines")}
    data = {"name": name}

    # Send POST request to the /dataset endpoint
    response = await client.post("/v0/dataset", headers=headers, data=data, files=files)

    # Assert the response
    assert response.status_code == 200
    assert response.json()["info"] == "Dataset uploaded succesfully!"
    assert blob_exists("uploaded_datasets", blob_name)

    # Send DELETE request to the /dataset endpoint
    response = await client.delete("/v0/dataset", headers=headers, params=data)

    # Assert the response
    assert response.status_code == 200
    assert response.json()["info"] == "Dataset deleted succesfully!"
    assert not dir_exists("uploaded_datasets", f"{user_id}/{name}/")


# TODO: Test list

# TODO: Test download
