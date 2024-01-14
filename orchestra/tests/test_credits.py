import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from starlette import status


@pytest.mark.anyio
async def test_credits(client: AsyncClient, fastapi_app: FastAPI) -> None:  # noqa: WPS218, E501
    """
    Checks the credits endpoint.

    :param client: client for the app.
    :param fastapi_app: current FastAPI application.
    """
    url = fastapi_app.url_path_for("get_credits")
    headers = {
        "accept": "application/json",
        "Authorization": "Bearer foI1elDa24CgSyCJWtPQX1161dbLv4X6bLpTJEDkFBQ=",
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
