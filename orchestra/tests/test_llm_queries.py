import json

import pytest
from httpx import AsyncClient
from starlette import status

from orchestra.tests.utils import (
    HEADERS,
    check_text_gen_choice,
    check_text_gen_response,
    check_text_gen_usage,
    get_chat_completions_arrow_payload_fallback,
    get_chat_completions_payload,
    get_chat_completions_payload_fallback,
    get_chat_completions_payload_tool_use,
    get_credits,
)

MODELS = [
    "gpt-3.5-turbo@openai",
    "claude-3-haiku@anthropic",
    "llama-3-8b-chat@deepinfra",
    "llama-3.1-8b-chat@fireworks-ai",
    # "llama-3-8b-chat@replicate",
    "llama-3.1-8b-chat@together-ai",
    "llama-3-8b-chat@aws-bedrock",
    "mistral-small@mistral-ai",
    # "gemini-1.5-flash@vertex-ai",
    "llama-3.1-8b-chat@groq",
    "deepseek-v3@deepseek",
    "grok-4@xai",
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
    models = ["claude-3-haiku@anthropic", "llama-3.1-8b-chat@deepinfra"]
    endpoint = "/v0/chat/completions"
    data = get_chat_completions_payload_fallback(models, stream=False)
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
    models = ["FAKE_MODEL@anthropic", "llama-3.1-8b-chat@deepinfra"]
    endpoint = "/v0/chat/completions"
    data = get_chat_completions_payload_fallback(models, stream=False)
    response = await client.post(endpoint, headers=HEADERS, json=data)
    assert response.status_code == status.HTTP_200_OK
    response_json = json.loads(response.text)
    assert response_json["model"] == "llama-3.1-8b-chat@deepinfra"
    check_text_gen_response(response_json, "chat.completion")
    check_text_gen_choice(response_json.get("choices")[0], "message")
    check_text_gen_usage(response_json.get("usage"))


@pytest.mark.anyio
async def test_arrow_fallback_parse(
    client: AsyncClient,
) -> None:
    model_str = (
        "claude-3-haiku->claude-3-sonnet@anthropic->llama-3.1-8b-chat@openai->deepinfra"
    )
    endpoint = "/v0/chat/completions"
    data = get_chat_completions_arrow_payload_fallback(model_str, stream=False)
    response = await client.post(endpoint, headers=HEADERS, json=data)
    assert response.status_code == status.HTTP_200_OK
    response_json = json.loads(response.text)
    check_text_gen_response(response_json, "chat.completion")
    check_text_gen_choice(response_json.get("choices")[0], "message")
    check_text_gen_usage(response_json.get("usage"))


@pytest.mark.anyio
async def test_arrow_fallback_after_fail(
    client: AsyncClient,
) -> None:
    model_str = "FAKE_MODEL@anthropic->llama-3.1-8b-chat@deepinfra"
    endpoint = "/v0/chat/completions"
    data = get_chat_completions_arrow_payload_fallback(model_str, stream=False)
    response = await client.post(endpoint, headers=HEADERS, json=data)
    assert response.status_code == status.HTTP_200_OK
    response_json = json.loads(response.text)
    assert response_json["model"] == "llama-3.1-8b-chat@deepinfra"
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
async def test_chat_completions_logging(client: AsyncClient):
    # 1. Call chat/completions
    payload = get_chat_completions_payload("gpt-3.5-turbo", "openai", stream=False)
    await client.post("/v0/chat/completions", headers=HEADERS, json=payload)
    # 2. Fetch logs for Usage
    resp = await client.get(
        "/v0/logs?project=Usage",
        headers=HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["logs"]) == 1
    evt = data["logs"][0]
    for key in ["model_provider_str", "query_body", "response_body", "credits"]:
        assert key in evt["entries"]


@pytest.mark.anyio
async def test_no_query_logging_when_disabled(client: AsyncClient):
    # 1. Fetch the current Usage logs count
    payload = get_chat_completions_payload("gpt-3.5-turbo", "openai", stream=False)
    await client.post("/v0/chat/completions", headers=HEADERS, json=payload)
    resp_before = await client.get("/v0/logs?project=Usage", headers=HEADERS)
    assert resp_before.status_code == 200
    count_before = len(resp_before.json()["logs"])

    # 2. Disable query logging
    disable_resp = await client.patch(
        "/v0/user/query-logging",
        headers=HEADERS,
        json={"enabled": False},
    )
    assert disable_resp.status_code == status.HTTP_200_OK
    assert disable_resp.json()["enabled"] is False

    # 3. Make a chat/completions request
    payload = get_chat_completions_payload("gpt-3.5-turbo", "openai", stream=False)
    llm_resp = await client.post("/v0/chat/completions", headers=HEADERS, json=payload)
    assert llm_resp.status_code == status.HTTP_200_OK

    # 4. Fetch the Usage logs again and assert no new entries
    resp_after = await client.get("/v0/logs?project=Usage", headers=HEADERS)
    assert resp_after.status_code == 200
    count_after = len(resp_after.json()["logs"])
    assert count_after == count_before


if __name__ == "__main__":
    pass
