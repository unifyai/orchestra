import json

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from starlette import status

from orchestra.tests.utils import HEADERS


@pytest.mark.anyio
async def test_endpoints_of_model(  # noqa: WPS218, E501
    client: AsyncClient,
    fastapi_app: FastAPI,
):
    url = "/v0/endpoints"
    params = {"model": "gpt-3.5-turbo"}
    response = await client.get(url, params=params, headers=HEADERS)
    assert response.status_code == status.HTTP_200_OK
    response_dict = json.loads(response.text)
    assert isinstance(response_dict, list)
    assert response_dict == ["gpt-3.5-turbo@openai"]


@pytest.mark.anyio
async def test_endpoints_of_provider(  # noqa: WPS218, E501
    client: AsyncClient,
    fastapi_app: FastAPI,
):
    url = "/v0/endpoints"
    params = {"provider": "aws-bedrock"}
    response = await client.get(url, params=params, headers=HEADERS)
    assert response.status_code == status.HTTP_200_OK
    response_dict = json.loads(response.text)
    assert isinstance(response_dict, list)
    assert response_dict == [
        "llama-2-13b-chat@aws-bedrock",
        "llama-3-8b-chat@aws-bedrock",
    ]


@pytest.mark.anyio
async def test_endpoints_of_all(  # noqa: WPS218, E501
    client: AsyncClient,
    fastapi_app: FastAPI,
):
    url = "/v0/endpoints"
    response = await client.get(url, headers=HEADERS)
    assert response.status_code == status.HTTP_200_OK
    for endpoint in ["llama-3-8b-chat@together-ai", "llama-3-8b-chat@deepinfra"]:
        assert endpoint in json.loads(response.text)


@pytest.mark.anyio
async def test_endpoints_of_overspecified(  # noqa: WPS218, E501
    client: AsyncClient,
    fastapi_app: FastAPI,
):
    url = "/v0/endpoints"
    params = {"model": "gpt-4o", "provider": "openai"}
    response = await client.get(url, params=params, headers=HEADERS)
    assert response.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.anyio
async def test_provider_from_model(  # noqa: WPS218, E501
    client: AsyncClient,
    fastapi_app: FastAPI,
):
    url = "/v0/providers"
    params = {"model": "gpt-3.5-turbo"}
    response = await client.get(url, params=params, headers=HEADERS)
    assert response.status_code == status.HTTP_200_OK
    response_dict = json.loads(response.text)
    assert isinstance(response_dict, list)
    assert response_dict == ["openai"]


@pytest.mark.anyio
async def test_providers_all(  # noqa: WPS218, E501
    client: AsyncClient,
    fastapi_app: FastAPI,
):
    url = "/v0/providers"
    response = await client.get(url, headers=HEADERS)
    assert response.status_code == status.HTTP_200_OK
    response_dict = json.loads(response.text)
    assert isinstance(response_dict, list)
    assert len(response_dict) > 1


@pytest.mark.anyio
async def test_models_from_provider(  # noqa: WPS218, E501
    client: AsyncClient,
    fastapi_app: FastAPI,
):
    url = "/v0/models"
    params = {"provider": "aws-bedrock"}
    response = await client.get(url, params=params, headers=HEADERS)
    assert response.status_code == status.HTTP_200_OK
    response_list = json.loads(response.text)
    assert isinstance(response_list, list)
    assert response_list == ["llama-2-13b-chat", "llama-3-8b-chat"]


@pytest.mark.anyio
async def test_models_all(  # noqa: WPS218, E501
    client: AsyncClient,
    fastapi_app: FastAPI,
):
    url = "/v0/models"
    response = await client.get(url, headers=HEADERS)
    assert response.status_code == status.HTTP_200_OK
    response_dict = response.json()
    assert isinstance(response_dict, list)
    assert len(response_dict) > 1
