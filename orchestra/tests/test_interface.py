import json
import os

import pytest
from httpx import AsyncClient

# Common headers and data
api_key = str(os.getenv("AUTH_ACCOUNT_API_KEY"))
HEADERS = {
    "accept": "application/json",
    "Authorization": f"Bearer {api_key}",
}


def _create_interface(client: AsyncClient, items, new_counter):
    return client.post(
        "/v0/interface",
        headers=HEADERS,
        json={
            "items": json.dumps(items),
            "new_counter": new_counter,
        },
    )


@pytest.mark.anyio
async def test_create_interface(client: AsyncClient):
    items = [
        {"i": "n0", "x": 0, "y": 0, "w": 3, "h": 3, "tab": None},
    ]
    new_counter = 1
    response = await _create_interface(client, items, new_counter)
    assert response.status_code == 200
    assert response.json()["info"] == "Interface created successfully!"


@pytest.mark.anyio
async def test_update_interface(client: AsyncClient):
    items = [
        {"i": "n0", "x": 0, "y": 0, "w": 3, "h": 3, "tab": None},
        {"i": "n1", "x": 0, "y": 1, "w": 2, "h": 3, "tab": "Plot_1"},
    ]
    new_counter = 2
    response = await client.put(
        "/v0/interface",
        headers=HEADERS,
        json={
            "items": json.dumps(items),
            "new_counter": new_counter,
        },
    )
    assert response.status_code == 200
    assert response.json()["info"] == "Interface updated successfully!"


@pytest.mark.anyio
async def test_get_interface(client: AsyncClient):
    items = [
        {"i": "n0", "x": 0, "y": 0, "w": 3, "h": 3, "tab": None},
    ]
    new_counter = 1
    await _create_interface(client, items, new_counter)
    response = await client.get("/v0/interface", headers=HEADERS)
    assert response.status_code == 200
    assert isinstance(response.json(), dict)
