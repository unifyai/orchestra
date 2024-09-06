import asyncio
import datetime
import json
import os

import pytest
from httpx import AsyncClient

from orchestra.tests.utils import HEADERS

api_key = str(os.getenv("AUTH_ACCOUNT_API_KEY"))

HEADERS = {
    "accept": "application/json",
    "Authorization": f"Bearer {api_key}",
}


def upload_benchmark(client, endpoint_name, metric_name, value):
    url = "/v0/benchmark"
    params = {
        "endpoint_name": endpoint_name,
        "metric_name": metric_name,
        "value": value,
    }
    response = client.post(url, params=params, headers=HEADERS)
    return response


def _custom_key_in_list(key, list):
    present = False
    print(list)
    for k in list:
        if k["name"] == key:
            present = True
    return present


@pytest.mark.anyio
async def test_custom_api_keys(  # noqa: WPS218, E501
    client: AsyncClient,
):

    # create custom api key
    url = "v0/custom_api_key"
    params = {"name": "key_1_test", "value": "1234"}
    response = await client.post(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()

    # get custom api key
    url = "v0/custom_api_key"
    response = await client.get(url, params={"name": "key_1_test"}, headers=HEADERS)
    assert json.loads(response.text) == {"name": "key_1_test", "value": "****1234"}

    # list custom api key
    url = "v0/custom_api_key/list"
    response = await client.get(url, headers=HEADERS)
    assert _custom_key_in_list("key_1_test", json.loads(response.text))

    # rename the api key
    url = "v0/custom_api_key/rename"
    params = {"name": "key_1_test", "new_name": "renamed_test"}
    response = await client.post(url, params=params, headers=HEADERS)
    url = "v0/custom_api_key/list"
    response = await client.get(url, headers=HEADERS)
    assert not _custom_key_in_list("key_1_test", json.loads(response.text))
    assert _custom_key_in_list("renamed_test", json.loads(response.text))

    # delete the api key
    url = "v0/custom_api_key"
    params = {"name": "renamed_test"}
    response = await client.delete(url, params=params, headers=HEADERS)
    url = "v0/custom_api_key/list"
    response = await client.get(url, headers=HEADERS)
    assert not _custom_key_in_list("renamed_test", json.loads(response.text))


@pytest.mark.anyio
async def test_custom_endpoints(  # noqa: WPS218, E501
    client: AsyncClient,
):

    # create custom api key
    url = "v0/custom_api_key"
    params = {"name": "key_2_test", "value": "1234"}
    response = await client.post(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()

    # create custom endpoint
    # TODO: Finetuned providers are not implemented
    url = "v0/custom_endpoint"
    params = {
        "name": "endpoint_name",
        "url": "https://url.com",
        "key_name": "key_2_test",
        "model_name": "model_name",
        # "provider": "existing_provider",
    }
    endpoint_info = {
        "name": "endpoint_name",
        "url": "https://url.com",
        "key": "key_2_test",
        "mdl_name": "model_name",
        # "provider": "existing_provider",
    }
    response = await client.post(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()

    # list custom endpoints
    url = "v0/custom_endpoint/list"
    response = await client.get(url, headers=HEADERS)
    assert endpoint_info in json.loads(response.text)

    # rename the endpoint
    url = "v0/custom_endpoint/rename"
    params = {"name": "endpoint_name", "new_name": "new_endpoint_name"}
    response = await client.post(url, params=params, headers=HEADERS)
    url = "v0/custom_endpoint/list"
    response = await client.get(url, headers=HEADERS)
    assert endpoint_info not in json.loads(response.text)
    endpoint_info["name"] = "new_endpoint_name"
    assert endpoint_info in json.loads(response.text)

    # delete the endpoint
    url = "v0/custom_endpoint"
    params = {"name": "new_endpoint_name"}
    response = await client.delete(url, params=params, headers=HEADERS)
    url = "v0/custom_endpoint/list"
    response = await client.get(url, headers=HEADERS)
    assert endpoint_info not in json.loads(response.text)


@pytest.mark.anyio
async def test_custom_benchmark(  # noqa: WPS218, E501
    client: AsyncClient,
):

    url = "v0/custom_api_key"
    params = {
        "name": "dummy_key",
        "value": "1234",
    }
    response = await client.post(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()

    # create custom endpoint
    url = "v0/custom_endpoint"
    params = {
        "name": "test_custom_endpoint",
        "url": "https://",
        "key_name": "dummy_key",
    }
    response = await client.post(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()

    endpoint_name = "test_custom_endpoint"
    url = "/v0/benchmark"
    params = {
        "endpoint_name": endpoint_name,
        "metric_name": "time-to-first-token",
        "value": 135,
    }

    response = await client.post(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()

    # now list them
    endpoint_name = "test_custom_endpoint"

    url = "/v0/benchmark"
    params = {
        "model": endpoint_name,
        "provider": "custom",
        "start_time": "2024-01-01",
        "end_time": str(datetime.datetime.now()),
    }
    response = await client.get(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()
    assert len(response.json()) > 0
    assert "ttft" in response.json()[0]
    assert response.json()[0]["ttft"] == 135


async def test_custom_benchmark_get_latest(  # noqa: WPS218, E501
    client: AsyncClient,
):

    url = "v0/custom_api_key"
    params = {
        "name": "dummy_key",
        "value": "1234",
    }
    response = await client.post(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()

    # create custom endpoint
    endpoint_name = "test_custom_endpoint"
    url = "v0/custom_endpoint"
    params = {
        "name": endpoint_name,
        "url": "https://",
        "key_name": "dummy_key",
    }
    response = await client.post(url, params=params, headers=HEADERS)
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
    url = "/v0/benchmark"
    params = {
        "model": endpoint_name,
        "provider": "custom",
    }
    response = await client.get(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()
    assert response.json()[0]["ttft"] == 133
    assert response.json()[0]["itl"] == 500
