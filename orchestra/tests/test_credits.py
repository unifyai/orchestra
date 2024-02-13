import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from starlette import status


@pytest.mark.anyio
def test_credits(  # noqa: WPS218, E501
    client: AsyncClient,
    fastapi_app: FastAPI,
) -> float:
    """
    Checks the credits endpoint.

    :param client: client for the app.
    :param fastapi_app: current FastAPI application.

    :return: credits.
    """
    from orchestra.tests.utils import HEADERS  # noqa: WPS433

    url = fastapi_app.url_path_for("get_credits")

    response = client.get(url, headers=HEADERS)
    assert response.status_code == status.HTTP_200_OK
    response_dict = response.json()
    assert isinstance(response_dict, dict)
    assert "credits" in response_dict
    assert isinstance(response_dict["credits"], float)
    assert "id" in response_dict
    assert isinstance(response_dict["id"], str)

    return response_dict["credits"]
