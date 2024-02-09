from unittest.mock import patch
import pytest
import json
from fastapi import FastAPI
from httpx import AsyncClient
from starlette import status

from orchestra.tests.utils import (
    HEADERS,
    get_chat_completions_payload,
    get_inference_payload,
    get_credits,
    check_text_gen_choice,
    check_text_gen_response,
    check_text_gen_usage,
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
