import datetime
import pytest
from httpx import AsyncClient
import asyncio

from orchestra.tests.utils import HEADERS
import os

api_key = str(os.getenv("AUTH_ACCOUNT_API_KEY"))

HEADERS = {
    "accept": "application/json",
    "Authorization": f"Bearer {api_key}",
}


def upload_benchmark(client, endpoint_name, metric_name, value):
    url = "/v0/custom_endpoint/benchmark"
    params = {
        "endpoint_name": endpoint_name,
        "metric_name": metric_name,
        "value": value,
    }
    response = client.post(url, params=params, headers=HEADERS)
    return response


@pytest.mark.anyio
async def test_custom_benchmark(  # noqa: WPS218, E501
    client: AsyncClient,
):

    url = "v0/custom_api_key"
    params = {
        "key": "dummy_key",
        "value": "1234",
    }
    response = await client.put(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()

    # create custom endpoint
    url = "v0/custom_endpoint"
    params = {
        "name": "test_custom_endpoint",
        "url": "https://",
        "key_name": "dummy_key",
    }
    response = await client.put(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()

    endpoint_name = "test_custom_endpoint"
    url = "/v0/custom_endpoint/benchmark"
    params = {
        "endpoint_name": endpoint_name,
        "metric_name": "time-to-first-token",
        "value": 135,
    }

    response = await client.post(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()

    # now list them
    endpoint_name = "test_custom_endpoint"

    url = "/v0/custom_endpoint/get_benchmark"
    params = {
        "endpoint_name": endpoint_name,
        "metric_name": "time-to-first-token",
        "start_time": "2024-01-01",
        "end_time": str(datetime.datetime.now()),
    }
    response = await client.get(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()
    assert len(response.json()) > 0
    assert "value" in response.json()[0]


async def test_custom_benchmark_get_latest(  # noqa: WPS218, E501
    client: AsyncClient,
):

    url = "v0/custom_api_key"
    params = {
        "key": "dummy_key",
        "value": "1234",
    }
    response = await client.put(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()

    # create custom endpoint
    endpoint_name = "test_custom_endpoint"
    url = "v0/custom_endpoint"
    params = {
        "name": endpoint_name,
        "url": "https://",
        "key_name": "dummy_key",
    }
    response = await client.put(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()

    # upload benchmarks
    response = await upload_benchmark(client, endpoint_name, "time-to-first-token", 135)
    assert response.status_code == 200, response.json()

    response = await upload_benchmark(client, endpoint_name, "inter-token-latency", 500)
    assert response.status_code == 200, response.json()

    await asyncio.sleep(5)
    response = await upload_benchmark(client, endpoint_name, "time-to-first-token", 133)
    assert response.status_code == 200, response.json()

    # check we return latest
    url = "/v0/benchmarks"
    params = {
        "model": endpoint_name,
        "provider": "custom",
    }
    response = await client.get(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()
    assert response.json()["ttft"] == 133
    assert response.json()["itl"] == 500
