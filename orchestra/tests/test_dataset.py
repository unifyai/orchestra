import os

import pytest
from httpx import AsyncClient

# Common headers and data
api_key = str(os.getenv("AUTH_ACCOUNT_API_KEY"))
HEADERS = {
    "accept": "application/json",
    "Authorization": f"Bearer {api_key}",
}


# Helper functions
def _create_dataset(client, dataset_name):
    return client.post("/v0/datasetv2", json={"name": dataset_name}, headers=HEADERS)


def _populate_dataset(client, dataset_name):
    return client.post(
        f"/v0/datasetv2/{dataset_name}/entries",
        json=["string entry", 123, True],
        headers=HEADERS,
    )


@pytest.mark.anyio
async def test_list_datasets(client: AsyncClient):
    dataset_name = "dir/subdir/dataset1"
    await _create_dataset(client, dataset_name)

    response = await client.get("/v0/datasetsv2/", headers=HEADERS)
    assert response.status_code == 200, response.json()
    assert isinstance(response.json(), list)
    assert any(dataset["name"] == dataset_name for dataset in response.json())


@pytest.mark.anyio
async def test_get_dataset_entries(client: AsyncClient):
    dataset_name = "dir/subdir/dataset1"
    await _create_dataset(client, dataset_name)
    await _populate_dataset(client, dataset_name)

    response = await client.get(
        f"/v0/datasetv2/{dataset_name}",
        headers=HEADERS,
        params={"limit": 10, "offset": 0},
    )
    assert response.status_code == 200, response.json()
    assert isinstance(response.json(), list)
    assert len(response.json()) == 3


@pytest.mark.anyio
async def test_get_dataset_entries_not_found(client: AsyncClient):
    dataset_name = "dir/subdir/nonexistent_dataset"
    response = await client.get(
        f"/v0/datasetv2/{dataset_name}",
        headers=HEADERS,
        params={"limit": 10, "offset": 0},
    )
    assert response.status_code == 404, response.json()
    assert response.json() == {"detail": "Dataset not found."}


@pytest.mark.anyio
async def test_create_dataset(client: AsyncClient):
    dataset_name = "dir/subdir/new_dataset"
    response = await _create_dataset(client, dataset_name)
    assert response.status_code == 201, response.json()
    assert response.json() == {"info": "Dataset created successfully!"}


@pytest.mark.anyio
async def test_create_existing_dataset(client: AsyncClient):
    dataset_name = "dir/subdir/existing_dataset"
    await _create_dataset(client, dataset_name)

    # Try to create the same dataset again
    response = await _create_dataset(client, dataset_name)
    assert response.status_code == 400, response.json()
    assert response.json() == {"detail": "Dataset already exists"}


@pytest.mark.anyio
async def test_delete_dataset(client: AsyncClient):
    dataset_name = "dir/subdir/dataset_to_delete"
    await _create_dataset(client, dataset_name)

    response = await client.delete(
        f"/v0/datasetv2/{dataset_name}",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    assert response.json() == "Dataset deleted successfully!"


@pytest.mark.anyio
async def test_delete_dataset_not_found(client: AsyncClient):
    dataset_name = "dir/subdir/nonexistent_dataset"
    response = await client.delete(
        f"/v0/datasetv2/{dataset_name}",
        headers=HEADERS,
    )
    assert response.status_code == 404, response.json()
    assert response.json() == {"detail": "Dataset not found."}


@pytest.mark.anyio
async def test_delete_dataset_entry(client: AsyncClient):
    dataset_name = "dir/subdir/dataset_with_entry"
    _ = await _create_dataset(client, dataset_name)
    ids = await _populate_dataset(client, dataset_name)

    # check 3 entries
    response = await client.get(
        f"/v0/datasetv2/{dataset_name}",
        headers=HEADERS,
    )
    assert len(response.json()) == 3

    _id = ids.json()["added"][0]
    response = await client.delete(
        f"/v0/datasetv2/{dataset_name}/entry/{_id}",
        headers=HEADERS,
    )
    assert response.status_code == 200

    # check 2 entries
    response = await client.get(
        f"/v0/datasetv2/{dataset_name}",
        headers=HEADERS,
    )
    assert len(response.json()) == 2


@pytest.mark.anyio
async def test_delete_dataset_entry_not_found(client: AsyncClient):
    dataset_name = "dir/subdir/dataset_with_entry"
    entry_id = "nonexistent_entry"
    await _create_dataset(client, dataset_name)

    response = await client.delete(
        f"/v0/datasetv2/{dataset_name}/entry/{entry_id}",
        headers=HEADERS,
    )
    assert response.status_code == 404, response.json()
    assert response.json() == {"detail": f"Dataset entry {entry_id} not found"}
