import json

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from starlette import status

from orchestra.tests.test_credits import test_credits


@pytest.mark.anyio
async def test_chat_completions_base_no_streaming(  # noqa: WPS218, E501
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
    headers = {
        "accept": "application/json",
        "Authorization": "Bearer mulv3oHXCvkUsodxgNgUbJJdbcu4XbP5NDEa4xk3wf8=",
        "Content-Type": "application/json",
    }
    data = {
        "model": "llama-2-7b-chat@anyscale",
        "messages": [
            {
                "role": "user",
                "content": "Explain who Newton was and his entire theory of"
                " gravitation. Give a long detailed response please"
                "and explain all of his achievements",
            },
        ],
        "stream": False,
        "max_tokens": 300,
    }

    response = await client.post(url, headers=headers, json=data)

    assert response.status_code == status.HTTP_200_OK
    response_data = response.json()

    assert "model" in response_data
    model, provider = response_data["model"].split("@")

    assert isinstance(model, str)
    assert isinstance(provider, str)

    assert "created" in response_data
    assert isinstance(response_data["created"], int)

    assert "id" in response_data
    assert isinstance(response_data["id"], str)

    assert "choices" in response_data
    assert isinstance(response_data["choices"], list)

    choices = response_data["choices"]
    first_choice = choices[0]

    assert "finish_reason" in first_choice
    assert isinstance(first_choice["finish_reason"], str)

    assert "index" in first_choice
    assert isinstance(first_choice["index"], int)

    assert "message" in first_choice
    assert isinstance(first_choice["message"], dict)

    message = first_choice["message"]

    assert "content" in message
    assert isinstance(message["content"], str)

    assert "role" in message
    assert isinstance(message["role"], str)

    assert "object" in response_data
    assert isinstance(response_data["object"], str)

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
async def test_chat_completions_octoai_no_streaming(  # noqa: WPS218, E501
    client: AsyncClient,
    fastapi_app: FastAPI,
) -> None:
    """
    Checks the chat completions endpoint for octoai, no streaming.

    :param client: client for the app.
    :param fastapi_app: current FastAPI application.
    """
    current_credits = await test_credits(client, fastapi_app)
    url = fastapi_app.url_path_for("get_completions")
    headers = {
        "accept": "application/json",
        "Authorization": "Bearer mulv3oHXCvkUsodxgNgUbJJdbcu4XbP5NDEa4xk3wf8=",
        "Content-Type": "application/json",
    }
    data = {
        "model": "mistral-7b-instruct-v0.1@octoai",
        "messages": [
            {
                "role": "user",
                "content": "Explain who Newton was and his entire theory of"
                " gravitation. Give a long detailed response please"
                "and explain all of his achievements",
            },
        ],
        "stream": False,
        "max_tokens": 300,
    }
    response = await client.post(url, headers=headers, json=data)

    assert response.status_code == status.HTTP_200_OK
    response_data = response.json()

    assert "model" in response_data

    model, provider = response_data["model"].split("@")
    assert isinstance(model, str)
    assert isinstance(provider, str)

    assert "created" in response_data
    assert isinstance(response_data["created"], int)

    assert "id" in response_data
    assert isinstance(response_data["id"], str)

    assert "choices" in response_data
    assert isinstance(response_data["choices"], list)

    choices = response_data["choices"]
    first_choice = choices[0]

    assert "finish_reason" in first_choice
    assert isinstance(first_choice["finish_reason"], str)

    assert "index" in first_choice
    assert isinstance(first_choice["index"], int)

    assert "message" in first_choice
    assert isinstance(first_choice["message"], dict)

    message = first_choice["message"]

    assert "content" in message
    assert isinstance(message["content"], str)

    assert "role" in message
    assert isinstance(message["role"], str)

    assert "object" in response_data
    assert isinstance(response_data["object"], str)

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
async def test_chat_completions_base_streaming(  # noqa: WPS218, E501
    client: AsyncClient,
    fastapi_app: FastAPI,
) -> None:
    """
    Checks the chat completions endpoint for litellm providers, with streaming.

    :param client: client for the app.
    :param fastapi_app: current FastAPI application.
    """
    current_credits = await test_credits(client, fastapi_app)
    url = fastapi_app.url_path_for("get_completions")
    headers = {
        "accept": "application/json",
        "Authorization": "Bearer mulv3oHXCvkUsodxgNgUbJJdbcu4XbP5NDEa4xk3wf8=",
        "Content-Type": "application/json",
    }
    data = {
        "model": "llama-2-7b-chat@anyscale",
        "messages": [
            {
                "role": "user",
                "content": "Explain who Newton was and his entire theory of"
                " gravitation. Give a long detailed response please"
                "and explain all of his achievements",
            },
        ],
        "stream": True,
        "max_tokens": 300,
    }

    response = await client.post(url, headers=headers, json=data)
    assert response.status_code == status.HTTP_200_OK
    for line in response.iter_lines():
        parts = line.split("}{")
        response_json = json.loads(f"{parts[0]}}}")

        assert "model" in response_json

        model, provider = response_json["model"].split("@")
        assert isinstance(model, str)
        assert isinstance(provider, str)

        assert "created" in response_json
        assert isinstance(response_json["created"], int)

        assert "id" in response_json
        assert isinstance(response_json["id"], str)

        assert "choices" in response_json
        assert isinstance(response_json["choices"], list)

        first_choice = response_json["choices"][0]

        assert "finish_reason" in first_choice
        assert first_choice["finish_reason"] is None or isinstance(
            first_choice["finish_reason"],
            str,
        )

        assert "index" in first_choice
        assert isinstance(first_choice["index"], int)

        assert "delta" in first_choice
        assert isinstance(first_choice["delta"], dict)

        delta = first_choice["delta"]

        assert "content" in delta
        assert isinstance(delta["content"], str)

        assert "role" in delta
        assert isinstance(delta["role"], str)

        assert "object" in response_json
        assert isinstance(response_json["object"], str)

        assert "usage" in response_json
        assert isinstance(response_json["usage"], dict)

        break

    final_credits = await test_credits(client, fastapi_app)
    assert final_credits < current_credits


@pytest.mark.anyio
async def test_chat_completions_base_octoai_streaming(  # noqa: WPS218, E501
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
    headers = {
        "accept": "application/json",
        "Authorization": "Bearer mulv3oHXCvkUsodxgNgUbJJdbcu4XbP5NDEa4xk3wf8=",
        "Content-Type": "application/json",
    }
    data = {
        "model": "mistral-7b-instruct-v0.1@octoai",
        "messages": [
            {
                "role": "user",
                "content": "Explain who Newton was and his entire theory of"
                " gravitation. Give a long detailed response please"
                "and explain all of his achievements",
            },
        ],
        "stream": True,
        "max_tokens": 300,
    }

    response = await client.post(url, headers=headers, json=data)
    assert response.status_code == status.HTTP_200_OK
    for line in response.iter_lines():
        parts = line.split("}{")
        response_json = json.loads(f"{{{parts[1]}}}")

        assert "model" in response_json

        model, provider = response_json["model"].split("@")
        assert isinstance(model, str)
        assert isinstance(provider, str)

        assert "created" in response_json
        assert isinstance(response_json["created"], int)

        assert "id" in response_json
        assert isinstance(response_json["id"], str)

        assert "choices" in response_json
        assert isinstance(response_json["choices"], list)

        first_choice = response_json["choices"][0]

        assert "finish_reason" in first_choice
        assert first_choice["finish_reason"] is None or isinstance(
            first_choice["finish_reason"],
            str,
        )

        assert "index" in first_choice
        assert isinstance(first_choice["index"], int)

        assert "delta" in first_choice
        assert isinstance(first_choice["delta"], dict)

        delta = first_choice["delta"]

        assert "content" in delta
        assert isinstance(delta["content"], str)

        assert "role" in delta
        assert isinstance(delta["role"], str)

        assert "object" in response_json
        assert isinstance(response_json["object"], str)

        break

    final_credits = await test_credits(client, fastapi_app)
    assert final_credits < current_credits
