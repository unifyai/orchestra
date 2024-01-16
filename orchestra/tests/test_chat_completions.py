import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from starlette import status

from orchestra.tests.test_credits import test_credits
from orchestra.tests.utils import (
    HEADERS,
    check_text_completion_no_streaming,
    check_text_completion_streaming,
    generate_data_chat_completions,
)

MODELS = ["llama-2-7b-chat@anyscale", "mistral-7b-instruct-v0.1@octoai"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "model",
    MODELS,
)
async def test_chat_completions_no_streaming(  # noqa: WPS218, E501
    model: str,
    client: AsyncClient,
    fastapi_app: FastAPI,
) -> None:
    """
    Checks the chat completions endpoint without streaming.

    :param client: client for the app.
    :param fastapi_app: current FastAPI application.
    """
    current_credits = await test_credits(client, fastapi_app)
    url = fastapi_app.url_path_for("get_completions")

    data = generate_data_chat_completions(model, stream=False)

    response = await client.post(url, headers=HEADERS, json=data)

    assert response.status_code == status.HTTP_200_OK

    await check_text_completion_no_streaming(
        response.json(),
        current_credits,
        client,
        fastapi_app,
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "model",
    MODELS,
)
async def test_chat_completions_streaming(  # noqa: WPS218, E501
    model: str,
    client: AsyncClient,
    fastapi_app: FastAPI,
) -> None:
    """
    Checks the chat completions endpoint with streaming.

    :param client: client for the app.
    :param fastapi_app: current FastAPI application.
    """
    current_credits = await test_credits(client, fastapi_app)
    url = fastapi_app.url_path_for("get_completions")

    data = generate_data_chat_completions(model, stream=True)

    response = await client.post(url, headers=HEADERS, json=data)

    await check_text_completion_streaming(
        model,
        response,
        current_credits,
        client,
        fastapi_app,
    )
