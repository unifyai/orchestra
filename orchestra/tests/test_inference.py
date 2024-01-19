import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from starlette import status

from orchestra.tests.test_credits import test_credits
from orchestra.tests.utils import (
    HEADERS,
    check_text_completion_no_streaming,
    check_text_completion_streaming,
    generate_data_inference_chat_completion,
)

MODELS = [
    "gpt-3.5-turbo@openai",
    "mistral-7b-instruct-v0.1@octoai",
    "llama-2-7b-chat@replicate",
]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "model",
    MODELS,
)
async def test_inference_text_completion_no_streaming(  # noqa: WPS218, E501
    model: str,
    client: AsyncClient,
    fastapi_app: FastAPI,
) -> None:
    """
    Checks the inference endpoint for text completion without streaming.

    :param client: client for the app.
    :param fastapi_app: current FastAPI application.
    """
    current_credits = await test_credits(client, fastapi_app)
    url = fastapi_app.url_path_for("get_inference")
    model, provider = model.split("@")

    data = generate_data_inference_chat_completion(model, provider, stream=False)

    response = await client.post(url, headers=HEADERS, json=data)

    assert response.status_code == status.HTTP_200_OK
    response_json = response.json()

    assert "response" in response_json
    assert isinstance(response_json["response"], dict)

    await check_text_completion_no_streaming(
        response_json["response"],
        current_credits,
        client,
        fastapi_app,
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "model",
    MODELS,
)
async def test_inference_text_completion_streaming(  # noqa: WPS218, E501
    model: str,
    client: AsyncClient,
    fastapi_app: FastAPI,
) -> None:
    """
    Checks the inference endpoint for text completion with streaming.

    :param client: client for the app.
    :param fastapi_app: current FastAPI application.
    """
    current_credits = await test_credits(client, fastapi_app)
    url = fastapi_app.url_path_for("get_inference")
    model_name, provider = model.split("@")

    data = generate_data_inference_chat_completion(model_name, provider, stream=True)

    response = await client.post(url, headers=HEADERS, json=data)
    await check_text_completion_streaming(
        model,
        response,
        current_credits,
        client,
        fastapi_app,
    )
