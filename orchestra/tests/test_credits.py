import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from starlette import status


@pytest.mark.anyio
async def test_credits(  # noqa: WPS218, E501
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
    headers = {
        "accept": "application/json",
        "Authorization": "Bearer mulv3oHXCvkUsodxgNgUbJJdbcu4XbP5NDEa4xk3wf8=",
        "Content-Type": "application/json",
    }

    response = await client.get(url, headers=headers)
    assert response.status_code == status.HTTP_200_OK
    response_dict = response.json()
    assert isinstance(response_dict, dict)
    assert "credits" in response_dict
    assert isinstance(response_dict["credits"], float)
    assert "id" in response_dict
    assert isinstance(response_dict["id"], str)

    return response_dict["credits"]
