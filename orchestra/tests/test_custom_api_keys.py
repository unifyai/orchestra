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
    assert response.status_code == 200, response.json()
    url = "v0/custom_api_key/list"
    response = await client.get(url, headers=HEADERS)
    assert not _custom_key_in_list("key_1_test", json.loads(response.text))
    assert _custom_key_in_list("renamed_test", json.loads(response.text))

    # delete the api key
    url = "v0/custom_api_key"
    params = {"name": "renamed_test"}
    response = await client.delete(url, params=params, headers=HEADERS)
    assert response.status_code == 200, response.json()
    url = "v0/custom_api_key/list"
    response = await client.get(url, headers=HEADERS)
    assert not _custom_key_in_list("renamed_test", json.loads(response.text))
