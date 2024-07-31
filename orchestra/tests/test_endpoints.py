import json
import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import text
from starlette import status

from orchestra.tests.utils import ADMIN_HEADERS, HEADERS


@pytest.mark.anyio
async def test_get_credits(  # noqa: WPS218, E501
    client: AsyncClient,
    fastapi_app: FastAPI,
) -> float:
    """
    Checks the credits endpoint.

    :param client: client for the app.
    :param fastapi_app: current FastAPI application.

    :return: credits.
    """
    url = fastapi_app.url_path_for("get_credits")

    response = await client.get(url, headers=HEADERS)
    assert response.status_code == status.HTTP_200_OK
    response_dict = response.json()
    assert isinstance(response_dict, dict)
    assert "credits" in response_dict
    assert isinstance(response_dict["credits"], float)
    assert "id" in response_dict
    assert isinstance(response_dict["id"], str)
    assert len(response_dict.keys()) == 2

    return response_dict["credits"]


@pytest.mark.anyio
async def test_stripe_customer_id(  # noqa: WPS218, E501
    client: AsyncClient,
    fastapi_app: FastAPI,
    dbsession,
) -> None:
    """Checks the stripe user id endpoint."""
    url = fastapi_app.url_path_for("update_user_stripe_customer_id")
    query = text("SELECT * FROM users WHERE users.id = 'stripe_autorecharge';")
    payload = {
        "id": "stripe_autorecharge",
        "stripe_customer_id": "stripe_id_1234",
    }

    pre = dbsession.execute(query).all()[0][2]
    assert pre == None
    response = await client.put(url, headers=ADMIN_HEADERS, params=payload)
    assert response.status_code == status.HTTP_200_OK
    post = dbsession.execute(query).all()[0][2]
    assert post == "stripe_id_1234"


@pytest.mark.anyio
async def test_enable_autorecharge(  # noqa: WPS218, E501
    client: AsyncClient,
    fastapi_app: FastAPI,
    dbsession,
) -> None:
    """Checks the enable autorecharge endpoint."""
    url = fastapi_app.url_path_for("update_user_autorecharge")
    query = text("SELECT * FROM users WHERE users.id = 'stripe_autorecharge';")
    payload_true = {
        "id": "stripe_autorecharge",
        "enable": "True",
    }
    payload_false = {
        "id": "stripe_autorecharge",
        "enable": "False",
    }

    pre = dbsession.execute(query).all()[0][3]
    assert pre == False
    response = await client.put(url, headers=ADMIN_HEADERS, params=payload_true)
    assert response.status_code == status.HTTP_200_OK
    post = dbsession.execute(query).all()[0][3]
    assert post == True
    response = await client.put(url, headers=ADMIN_HEADERS, params=payload_false)
    assert response.status_code == status.HTTP_200_OK
    post = dbsession.execute(query).all()[0][3]
    assert post == False


@pytest.mark.anyio
async def test_autorecharge_threshold(  # noqa: WPS218, E501
    client: AsyncClient,
    fastapi_app: FastAPI,
    dbsession,
) -> None:
    """Checks the autorecharge threshold endpoint."""
    url = fastapi_app.url_path_for("update_user_autorecharge_threshold")
    query = text("SELECT * FROM users WHERE users.id = 'stripe_autorecharge';")
    payload = {
        "id": "stripe_autorecharge",
        "threshold": 10,
    }

    pre = dbsession.execute(query).all()[0][4]
    assert pre == -1
    response = await client.put(url, headers=ADMIN_HEADERS, params=payload)
    assert response.status_code == status.HTTP_200_OK
    post = dbsession.execute(query).all()[0][4]
    assert post == 10


@pytest.mark.anyio
async def test_autorecharge_qty(  # noqa: WPS218, E501
    client: AsyncClient,
    fastapi_app: FastAPI,
    dbsession,
) -> None:
    """Checks the autorecharge qty endpoint."""
    url = fastapi_app.url_path_for("update_user_autorecharge_qty")
    query = text("SELECT * FROM users WHERE users.id = 'stripe_autorecharge';")
    payload = {
        "id": "stripe_autorecharge",
        "qty": "10",
    }

    pre = dbsession.execute(query).all()[0][5]
    assert pre == 0
    response = await client.put(url, headers=ADMIN_HEADERS, params=payload)
    assert response.status_code == status.HTTP_200_OK
    post = dbsession.execute(query).all()[0][5]
    assert post == 10


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
    assert response_dict == ["openai"]


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
    assert response_dict == ["llama-2-13b-chat", "llama-3-8b-chat"]


@pytest.mark.anyio
async def test_endpoints_of_all(  # noqa: WPS218, E501
    client: AsyncClient,
    fastapi_app: FastAPI,
):
    url = "/v0/endpoints"
    response = await client.get(url, headers=HEADERS)
    assert response.status_code == status.HTTP_200_OK
    for endpoint in ["llama-3-8b-chat@anyscale", "llama-3-8b-chat@deepinfra"]:
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
