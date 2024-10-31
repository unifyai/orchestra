import os

import pytest
from httpx import AsyncClient

api_key = str(os.getenv("AUTH_ACCOUNT_API_KEY"))

HEADERS = {
    "accept": "application/json",
    "Authorization": f"Bearer {api_key}",
}

artifact_data = {
    "artifacts": {
        "artifact_1": "value_1",
        "artifact_2": 2,
        "artifact_3": 3.0,
        "artifact_4": {"a": 4, 5: [6, 7.0]},
    },
}


def _create_artifacts(client, dataset_name):
    return client.post(
        f"/v0/dataset/{dataset_name}/artifacts",
        json=artifact_data,
        headers=HEADERS,
    )


def _create_dataset(client, dataset_name):
    url = "/v0/dataset"
    dataset_data = {"name": dataset_name}
    return client.post(url, json=dataset_data, headers=HEADERS)


@pytest.mark.anyio
async def test_create_artifacts(client: AsyncClient):
    dataset_name = "eval-dataset"
    _ = await _create_dataset(client, dataset_name)

    response = await _create_artifacts(client, dataset_name)

    assert response.status_code == 200, response.json()
    assert response.json() == {"info": "Artifact(s) created successfully!"}


@pytest.mark.anyio
async def test_create_artifacts_dataset_not_found(client: AsyncClient):
    dataset_name = "non_existent_dataset"

    # This should return 404 as the dataset does not exist
    response = await _create_artifacts(client, dataset_name)

    assert response.status_code == 404, response.json()
    assert response.json() == {"detail": f"Dataset {dataset_name} not found."}


@pytest.mark.anyio
async def test_delete_artifact(client: AsyncClient):
    dataset_name = "eval-dataset"
    artifact_key = "artifact_1"
    _ = await _create_dataset(client, dataset_name)
    _ = await _create_artifacts(client, dataset_name)

    # delete artifacts
    response = await client.delete(
        f"/v0/dataset/{dataset_name}/artifacts/{artifact_key}",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    assert response.json() == {"info": "Artifact deleted successfully!"}


@pytest.mark.anyio
async def test_delete_artifact_dataset_not_found(client: AsyncClient):
    dataset_name = "non_existent_dataset"
    artifact_key = "artifact_1"

    # This should return 404 as the dataset does not exist
    response = await client.delete(
        f"/v0/dataset/{dataset_name}/artifacts/{artifact_key}",
        headers=HEADERS,
    )
    assert response.status_code == 404, response.json()
    assert response.json() == {
        "detail": f"Dataset {dataset_name} not found.",
    }


@pytest.mark.anyio
async def test_delete_artifact_not_found(client: AsyncClient):
    dataset_name = "eval-dataset"
    artifact_key = "non_existent_artifact"
    _ = await _create_dataset(client, dataset_name)

    # This should return 404 as the artifact does not exist
    response = await client.delete(
        f"/v0/dataset/{dataset_name}/artifacts/{artifact_key}",
        headers=HEADERS,
    )
    assert response.status_code == 404, response.json()
    assert response.json() == {
        "detail": f"Artifact {artifact_key} not found.",
    }


@pytest.mark.anyio
async def test_list_artifacts(client: AsyncClient):
    dataset_name = "eval-dataset"
    _ = await _create_dataset(client, dataset_name)
    _ = await _create_artifacts(client, dataset_name)

    # This should return the list of artifacts
    response = await client.get(
        f"/v0/dataset/{dataset_name}/artifacts",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    assert isinstance(response.json(), dict)  # Ensure the response is a dictionary


@pytest.mark.anyio
async def test_list_artifacts_dataset_not_found(client: AsyncClient):
    dataset_name = "non_existent_dataset"

    # This should return 404 as the dataset does not exist
    response = await client.get(
        f"/v0/dataset/{dataset_name}/artifacts",
        headers=HEADERS,
    )
    assert response.status_code == 404, response.json()
    assert response.json() == {
        "detail": f"Dataset {dataset_name} not found.",
    }


if __name__ == "__main__":
    pass
