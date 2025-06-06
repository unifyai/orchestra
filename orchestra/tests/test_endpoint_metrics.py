import os

from orchestra.tests.utils import HEADERS

api_key = str(os.getenv("AUTH_ACCOUNT_API_KEY"))

HEADERS = {
    "accept": "application/json",
    "Authorization": f"Bearer {api_key}",
}


def upload_endpoint_metric(client, endpoint_name, metric_name, value):
    url = "/v0/endpoint-metrics"
    params = {
        "endpoint_name": endpoint_name,
        "metric_name": metric_name,
        "value": value,
    }
    response = client.post(url, params=params, headers=HEADERS)
    return response


# @pytest.mark.anyio
# async def test_custom_endpoint_metrics(  # noqa: WPS218, E501
#     client: AsyncClient,
# ):
#
#     url = "v0/custom_api_key"
#     params = {
#         "name": "dummy_key",
#         "value": "1234",
#     }
#     response = await client.post(url, params=params, headers=HEADERS)
#     assert response.status_code == 200, response.json()
#
#     # create custom endpoint
#     url = "v0/custom_endpoint"
#     params = {
#         "name": "test_custom_endpoint@custom",
#         "url": "https://",
#         "key_name": "dummy_key",
#     }
#     response = await client.post(url, params=params, headers=HEADERS)
#     assert response.status_code == 200, response.json()
#
#     endpoint_name = "test_custom_endpoint@custom"
#     url = "/v0/endpoint-metrics"
#     params = {
#         "endpoint_name": endpoint_name,
#         "metric_name": "ttft",
#         "value": 135,
#     }
#
#     response = await client.post(url, params=params, headers=HEADERS)
#     assert response.status_code == 200, response.json()
#
#     # now list them
#     endpoint_name = "test_custom_endpoint@custom"
#
#     url = "/v0/endpoint-metrics"
#     params = {
#         "model": endpoint_name.split("@")[0],
#         "provider": endpoint_name.split("@")[1],
#         "start_time": "2024-01-01",
#         "end_time": str(datetime.now(timezone.utc)),
#     }
#     response = await client.get(url, params=params, headers=HEADERS)
#     assert response.status_code == 200, response.json()
#     assert len(response.json()) > 0
#     assert "ttft" in response.json()[0]
#     assert response.json()[0]["ttft"] == 135
#
#     # Delete the metrics
#     url = "/v0/endpoint-metrics"
#     params = {
#         "endpoint_name": endpoint_name,
#     }
#     response = await client.delete(url, params=params, headers=HEADERS)
#     assert response.status_code == 200, response.json()
#
#     # List again, assert empty
#     endpoint_name = "test_custom_endpoint@custom"
#     url = "/v0/endpoint-metrics"
#     params = {
#         "model": endpoint_name.split("@")[0],
#         "provider": endpoint_name.split("@")[1],
#         "start_time": "2024-01-01",
#         "end_time": str(datetime.now(timezone.utc)),
#     }
#     response = await client.get(url, params=params, headers=HEADERS)
#     assert response.status_code == 200, response.json()
#     assert len(response.json()) == 0
#
#
# async def test_custom_endpoint_metrics_get_latest(  # noqa: WPS218, E501
#     client: AsyncClient,
# ):
#
#     url = "v0/custom_api_key"
#     params = {
#         "name": "dummy_key",
#         "value": "1234",
#     }
#     response = await client.post(url, params=params, headers=HEADERS)
#     assert response.status_code == 200, response.json()
#
#     # create custom endpoint
#     endpoint_name = "test_custom_endpoint@custom"
#     url = "v0/custom_endpoint"
#     params = {
#         "name": endpoint_name,
#         "url": "https://",
#         "key_name": "dummy_key",
#     }
#     response = await client.post(url, params=params, headers=HEADERS)
#     assert response.status_code == 200, response.json()
#
#     # upload benchmarks
#     response = await upload_endpoint_metric(
#         client,
#         endpoint_name,
#         "ttft",
#         135,
#     )
#     assert response.status_code == 200, response.json()
#
#     response = await upload_endpoint_metric(
#         client,
#         endpoint_name,
#         "itl",
#         500,
#     )
#     assert response.status_code == 200, response.json()
#
#     await asyncio.sleep(5)
#     response = await upload_endpoint_metric(
#         client,
#         endpoint_name,
#         "ttft",
#         133,
#     )
#     assert response.status_code == 200, response.json()
#
#     # check we return latest
#     url = "/v0/endpoint-metrics"
#     params = {
#         "model": endpoint_name.split("@")[0],
#         "provider": endpoint_name.split("@")[1],
#     }
#     response = await client.get(url, params=params, headers=HEADERS)
#     assert response.status_code == 200, response.json()
#     assert response.json()[0]["ttft"] == 133
#     assert response.json()[0]["itl"] == 500
#
#
# if __name__ == "__main__":
#     pass
#
