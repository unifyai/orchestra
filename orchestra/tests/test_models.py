import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from starlette import status


@pytest.mark.anyio
async def test_models(client: AsyncClient, fastapi_app: FastAPI) -> None:
    """
    Checks the models endpoint.

    :param client: client for the app.
    :param fastapi_app: current FastAPI application.
    """
    url = fastapi_app.url_path_for("list_models")
    response = await client.get(url)
    assert response.status_code == status.HTTP_200_OK
    sample_model_info = response.json()["models"][0]
    assert "id" in sample_model_info
    assert "modality" in sample_model_info
    assert "task" in sample_model_info
    assert len(sample_model_info["providers"])
