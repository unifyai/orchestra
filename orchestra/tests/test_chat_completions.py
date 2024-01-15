import json

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from starlette import status

from orchestra.tests.test_credits import test_credits
from orchestra.tests.utils import HEADERS, assert_model, generate_data_chat_completions


@pytest.mark.anyio
@pytest.mark.parametrize(
    "model",
    ["llama-2-7b-chat@anyscale", "mistral-7b-instruct-v0.1@octoai"],
)
async def test_chat_completions_no_streaming(  # noqa: WPS218, E501
    model: str,
    client: AsyncClient,
    fastapi_app: FastAPI,
) -> None:
    """
    Checks the chat completions endpoint for litellm providers, no streaming.

    :param client: client for the app.
    :param fastapi_app: current FastAPI application.
    """
    current_credits = await test_credits(client, fastapi_app)
    url = fastapi_app.url_path_for("get_completions")

    data = generate_data_chat_completions(model, stream=False)

    response = await client.post(url, headers=HEADERS, json=data)

    assert response.status_code == status.HTTP_200_OK
    response_data = response.json()

    first_choice = assert_model(response_data)

    assert "message" in first_choice
    assert isinstance(first_choice["message"], dict)

    message = first_choice["message"]

    assert "content" in message
    assert isinstance(message["content"], str)

    assert "role" in message
    assert isinstance(message["role"], str)

    assert "usage" in response_data
    assert isinstance(response_data["usage"], dict)

    usage_data = response_data["usage"]

    assert "completion_tokens" in usage_data
    assert isinstance(usage_data["completion_tokens"], int)

    assert "prompt_tokens" in usage_data
    assert isinstance(usage_data["prompt_tokens"], int)

    assert "total_tokens" in usage_data
    assert isinstance(usage_data["total_tokens"], int)

    final_credits = await test_credits(client, fastapi_app)
    assert final_credits < current_credits


@pytest.mark.anyio
@pytest.mark.parametrize(
    "model",
    ["llama-2-7b-chat@anyscale", "mistral-7b-instruct-v0.1@octoai"],
)
async def test_chat_completions_streaming(  # noqa: WPS218, E501
    model: str,
    client: AsyncClient,
    fastapi_app: FastAPI,
) -> None:
    """
    Checks the chat completions endpoint for octoai, with streaming.

    :param client: client for the app.
    :param fastapi_app: current FastAPI application.
    """
    current_credits = await test_credits(client, fastapi_app)
    url = fastapi_app.url_path_for("get_completions")

    data = generate_data_chat_completions(model, stream=True)

    response = await client.post(url, headers=HEADERS, json=data)
    assert response.status_code == status.HTTP_200_OK
    for line in response.iter_lines():
        parts = line.split("}{")
        if model.split("@")[1] == "octoai":
            response_json = json.loads(f"{{{parts[1]}}}")
        else:
            response_json = json.loads(f"{parts[0]}}}")

        first_choice = assert_model(response_json)

        assert "delta" in first_choice
        assert isinstance(first_choice["delta"], dict)

        delta = first_choice["delta"]

        assert "content" in delta
        assert isinstance(delta["content"], str)

        assert "role" in delta
        assert isinstance(delta["role"], str)

        break

    final_credits = await test_credits(client, fastapi_app)
    assert final_credits < current_credits
