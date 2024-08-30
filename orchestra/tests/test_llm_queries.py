import json

import pytest
from httpx import AsyncClient
from starlette import status

from orchestra.tests.utils import (
    HEADERS,
    check_text_gen_choice,
    check_text_gen_response,
    check_text_gen_usage,
    get_chat_completions_payload,
    get_chat_completions_payload_fallback,
    get_chat_completions_payload_tool_use,
    get_credits,
)

MODELS = [
    "gpt-3.5-turbo@openai",
    "claude-3-haiku@anthropic",
    "llama-3-8b-chat@deepinfra",
    "llama-3-8b-chat@fireworks-ai",
    "llama-3-8b-chat@lepton-ai",
    "llama-3-8b-chat@replicate",
    "llama-3-8b-chat@together-ai",
    "llama-3-8b-chat@aws-bedrock",
    "mistral-7b-instruct-v0.3@mistral-ai",
    "mistral-7b-instruct-v0.3@octoai",
    "gemini-1.5-flash@vertex-ai",
    "llama-3.1-8b-chat@perplexity-ai",
    "llama-3-8b-chat@groq",
    "llama-3.1-8b-chat@azure-ai",
]


@pytest.mark.anyio
@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("stream_str", ["stream", "standard"])
async def test_text_generation(  # noqa: WPS218, E501
    model: str,
    stream_str: str,
    client: AsyncClient,
) -> None:
    """
    Checks the text-generations endpoints.

    :param client: client for the app.
    """
    # TODO: Test max tokens and rest of parameters
    model, provider = model.split("@")

    stream = stream_str == "stream"
    data = get_chat_completions_payload(model, provider, stream=stream)

    pre_credits = await get_credits(client)
    response = await client.post("/v0/chat/completions", headers=HEADERS, json=data)
    assert response.status_code == status.HTTP_200_OK

    if stream:
        for line in response.iter_lines():
            if not line:
                continue
            response_dict = line.removeprefix("data: ")
            response_json = json.loads(response_dict)
            check_text_gen_response(response_json, "chat.completion.chunk")
            if response_json["usage"] != {}:
                continue
            check_text_gen_choice(response_json.get("choices")[0], "delta")
    else:
        response_json = json.loads(response.text)
        check_text_gen_response(response_json, "chat.completion")
        check_text_gen_choice(response_json.get("choices")[0], "message")
        check_text_gen_usage(response_json.get("usage"))

    usage_keys = ["cost", "prompt_tokens", "completion_tokens", "total_tokens"]
    assert all(k in response_json["usage"] for k in usage_keys)

    post_credits = await get_credits(client)
    assert post_credits < pre_credits


@pytest.mark.anyio
async def test_fallback_parse(
    client: AsyncClient,
) -> None:
    model_str = "claude-3-haiku@anthropic->mistral-7b-instruct-v0.3@octoai"
    endpoint = "/v0/chat/completions"
    data = get_chat_completions_payload_fallback(model_str, stream=False)
    response = await client.post(endpoint, headers=HEADERS, json=data)
    assert response.status_code == status.HTTP_200_OK
    response_json = json.loads(response.text)
    check_text_gen_response(response_json, "chat.completion")
    check_text_gen_choice(response_json.get("choices")[0], "message")
    check_text_gen_usage(response_json.get("usage"))


@pytest.mark.anyio
async def test_fallback_after_fail(
    client: AsyncClient,
) -> None:
    model_str = "FAKE_MODEL@anthropic->mistral-7b-instruct-v0.3@octoai"
    endpoint = "/v0/chat/completions"
    data = get_chat_completions_payload_fallback(model_str, stream=False)
    response = await client.post(endpoint, headers=HEADERS, json=data)
    assert response.status_code == status.HTTP_200_OK
    response_json = json.loads(response.text)
    assert response_json["model"] == "mistral-7b-instruct-v0.3@octoai"
    check_text_gen_response(response_json, "chat.completion")
    check_text_gen_choice(response_json.get("choices")[0], "message")
    check_text_gen_usage(response_json.get("usage"))


@pytest.mark.anyio
@pytest.mark.parametrize("model", ["gpt-3.5-turbo@openai", "claude-3-haiku@anthropic"])
async def test_function_calling(model, client: AsyncClient):
    endpoint = "/v0/chat/completions"
    data = get_chat_completions_payload_tool_use(model)
    response = await client.post(endpoint, headers=HEADERS, json=data)
    assert response.status_code == status.HTTP_200_OK

    response_json = response.json()
    assert len(response_json["choices"][0]["message"]["tool_calls"]) >= 1, str(
        response_json,
    )


@pytest.mark.anyio
@pytest.mark.parametrize("model", MODELS)
async def test_n_1(model, client: AsyncClient):
    model, provider = model.split("@")
    endpoint = "/v0/chat/completions"
    data = get_chat_completions_payload(model, provider, stream=False)
    data["n"] = 1
    response = await client.post(endpoint, headers=HEADERS, json=data)
    assert response.status_code == status.HTTP_200_OK


@pytest.mark.anyio
async def test_tags(client: AsyncClient):
    endpoint = "/v0/chat/completions"
    data = get_chat_completions_payload("llama-3-8b-chat", "aws-bedrock", stream=False)
    data["tags"] = ["dummy_tag"]
    response = await client.post(endpoint, headers=HEADERS, json=data)
    assert response.status_code == status.HTTP_200_OK

    endpoint = "/v0/queries"
    data = {"tags": ["dummy_tag"]}
    response = await client.get(endpoint, headers=HEADERS, params=data)
    assert response.status_code == 200, response.json()
    assert len(response.json()) == 1, response.json()

    endpoint = "/v0/tags"
    response = await client.get(endpoint, headers=HEADERS)
    assert response.status_code == 200, response.json()
    tags = response.json()
    assert isinstance(tags, list)
    assert len(tags) == 1
    assert "dummy_tag" in tags


@pytest.mark.anyio
async def test_tags_str_only(client: AsyncClient):
    endpoint = "/v0/chat/completions"
    data = get_chat_completions_payload("llama-3-8b-chat", "aws-bedrock", stream=False)
    data["tags"] = "dummy_tag_str"
    response = await client.post(endpoint, headers=HEADERS, json=data)
    assert response.status_code == status.HTTP_200_OK

    endpoint = "/v0/queries"
    data = {"tags": ["dummy_tag_str"]}
    response = await client.get(endpoint, headers=HEADERS, params=data)
    assert response.status_code == 200, response.json()
    assert len(response.json()) == 1, response.json()


@pytest.mark.anyio
async def test_fake_tags(client: AsyncClient):
    endpoint = "/v0/chat/completions"
    data = get_chat_completions_payload("llama-3-8b-chat", "aws-bedrock", stream=False)
    data["tags"] = ["dummy_tag1"]
    response = await client.post(endpoint, headers=HEADERS, json=data)
    assert response.status_code == status.HTTP_200_OK

    endpoint = "/v0/queries"
    data = {"tags": ["dummy_tag_FAKE"]}
    response = await client.get(endpoint, headers=HEADERS, params=data)
    assert response.status_code == 200, response.json()
    assert len(response.json()) == 0, response.json()
