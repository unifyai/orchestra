from unittest.mock import patch
import pytest
import json
from fastapi import FastAPI
from httpx import AsyncClient
from starlette import status
from typing import Any, Dict

from orchestra.tests.test_credits import test_credits
from orchestra.tests.utils import (
    HEADERS,
    get_chat_completions_payload,
    get_inference_payload,
)

MODELS = [
    "llama-2-7b-chat@anyscale",
    "llama-2-7b-chat@replicate",
    "mistral-7b-instruct-v0.1@octoai",
]

payload_fn = {
    "/v0/chat/completions": get_chat_completions_payload,
    "/v0/inference": get_inference_payload,
}


# TODO: Move to utils
def check_in_dict_and_instance(dict, key, types):
    assert key in dict
    assert isinstance(dict.get(key), types)


# TODO: Move this to utils
def check_text_gen_response(response: Dict, object_str: str):

    assert isinstance(response.get("id"), str)
    assert response.get("object") == object_str
    assert isinstance(response.get("created"), int)
    # TODO: We need to add a system_fingerprint
    # assert isinstance(response.get("system_fingerprint"), str)
    assert isinstance(response.get("choices"), list)
    assert isinstance(response.get("usage"), dict)

    if "provider" in response:
        model, provider = response.get("model"), response.get("provider")
    else:
        model, provider = response.get("model").split("@")

    assert isinstance(model, str)
    assert isinstance(provider, str)


# TODO: Move this to utils
def check_text_gen_usage(usage: Dict):
    assert isinstance(usage, dict)
    assert isinstance(usage.get("completion_tokens"), int)
    assert isinstance(usage.get("prompt_tokens"), int)
    assert isinstance(usage.get("total_tokens"), int)


# TODO: Move this to utils
def check_text_gen_choice(choice: Dict, message: str):
    assert message in ["message", "delta"]
    assert isinstance(choice, dict)
    assert isinstance(choice.get("index"), int)
    # TODO: Test this properly with all possible cases,
    # document the posibilities as well. We should have test
    # cases that detect max length, check for the last token, etc.
    check_in_dict_and_instance(choice, "finish_reason", (type(None), str))
    # TODO: Check if we are reading `null` correctly
    # TODO: When this is none, is the key included at all? are we not
    # including it?
    # check_in_dict_and_instance(choice, "logprobs", (None, bool))
    message = choice.get(message)
    assert isinstance(message, dict)
    if message == "full":
        assert isinstance(message.get("role"), str)
    # TODO: Add option for empty message if end of stream and delta
    assert isinstance(message.get("content"), str)


@pytest.mark.anyio
@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("endpoint", ["/v0/chat/completions", "/v0/inference"])
@pytest.mark.parametrize("stream_str", ["stream", "standard"])
async def test_text_generation(  # noqa: WPS218, E501
    model: str,
    endpoint: str,
    stream_str: str,
    client: AsyncClient,
    fastapi_app: FastAPI,
) -> None:
    """
    Checks the chat completions endpoint without streaming.

    :param client: client for the app.
    :param fastapi_app: current FastAPI application.
    """
    with patch("fastapi.BackgroundTasks.add_task") as mock:

        mock.side_effect = lambda *args, **kwargs: None
        model, provider = model.split("@")
        stream = stream_str == "stream"
        data = payload_fn[endpoint](model, provider, stream=stream)

        pre_credits = await test_credits(client, fastapi_app)
        response = await client.post(endpoint, headers=HEADERS, json=data)
        assert response.status_code == status.HTTP_200_OK

        if stream:
            # check stream
            for line in response.iter_lines():
                response_dict = line.removeprefix("data: ")
                response_json = json.loads(response_dict)
                check_text_gen_response(response_json, "chat.completion.chunk")
                check_text_gen_choice(response_json.get("choices")[0], "delta")
                break  # TODO: Remove this and test corner cases properly
        else:
            # check non-stream
            response_json = json.loads(response.text)
            check_text_gen_response(response_json, "chat.completion")
            check_text_gen_choice(response_json.get("choices")[0], "message")
            check_text_gen_usage(response_json.get("usage"))

        post_credits = await test_credits(client, fastapi_app)
        assert post_credits < pre_credits
