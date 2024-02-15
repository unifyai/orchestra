import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from starlette import status


@pytest.mark.anyio
def test_models(client: AsyncClient, fastapi_app: FastAPI) -> None:
    """
    Checks the models endpoint.

    :param client: client for the app.
    :param fastapi_app: current FastAPI application.
    """
    url = fastapi_app.url_path_for("get_models")
    response = client.get(url)
    assert response.status_code == status.HTTP_200_OK
