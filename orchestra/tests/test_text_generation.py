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
    get_credits,
    get_inference_payload,
)

MODELS = [
    # "gpt-3.5-turbo@openai", # OpenAI is not in the db seeding script
    "llama-2-7b-chat@anyscale",
    "llama-2-7b-chat@deepinfra",
    "llama-2-7b-chat@fireworks-ai",
    "llama-2-7b-chat@lepton-ai",
    "llama-2-7b-chat@replicate",
    "llama-2-7b-chat@together-ai",
    "llama-2-13b-chat@aws-bedrock",
    "mistral-7b-instruct-v0.2@aws-bedrock",
    "mistral-7b-instruct-v0.2@mistral-ai",
    "mistral-7b-instruct-v0.1@octoai",
    "mistral-7b-instruct-v0.2@perplexity-ai",
]

payload_fn = {
    "/v0/chat/completions": get_chat_completions_payload,
    "/v0/inference": get_inference_payload,
}


@pytest.mark.anyio
@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("endpoint", ["/v0/chat/completions", "/v0/inference"])
@pytest.mark.parametrize("stream_str", ["stream", "standard"])
async def test_text_generation(  # noqa: WPS218, E501
    model: str,
    endpoint: str,
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
    data = payload_fn[endpoint](model, provider, stream=stream)

    pre_credits = await get_credits(client)
    response = await client.post(endpoint, headers=HEADERS, json=data)
    assert response.status_code == status.HTTP_200_OK

    if stream:
        for line in response.iter_lines():
            response_dict = line.removeprefix("data: ")
            response_json = json.loads(response_dict)
            check_text_gen_response(response_json, "chat.completion.chunk")
            check_text_gen_choice(response_json.get("choices")[0], "delta")
            break  # TODO: Remove this and test corner cases properly
    else:
        response_json = json.loads(response.text)
        check_text_gen_response(response_json, "chat.completion")
        check_text_gen_choice(response_json.get("choices")[0], "message")
        check_text_gen_usage(response_json.get("usage"))

    post_credits = await get_credits(client)
    assert post_credits < pre_credits
