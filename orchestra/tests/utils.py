# TODO: Add extra parameters to the tests payload (_partial_openai_payload)
# TODO: Add logging to the tests to see the actual responses manually if needed
import os
from typing import Dict

api_key = str(os.getenv("AUTH_ACCOUNT_API_KEY"))
admin_api_key = str(os.getenv("ORCHESTRA_ADMIN_KEY"))

HEADERS = {
    "accept": "application/json",
    "Authorization": f"Bearer {api_key}",
    "Content-Type": "application/json",
}

ADMIN_HEADERS = {
    "accept": "application/json",
    "Authorization": f"Bearer {admin_api_key}",
    "Content-Type": "application/json",
}

prompt = (
    "Explain who Newton was and his entire theory of gravitation. "
    "Give a short response with line breaks within each sentence."
)


async def get_credits(client):
    response = await client.get("/v0/get_credits", headers=HEADERS)
    return response.json()["credits"]


def _partial_openai_payload(temperature=0.5, max_tokens=100, stream=False):
    return {
        "messages": [
            {
                "role": "system",
                "content": "You are an useful assitant",
            },
            {
                "role": "user",
                "content": prompt,
            },
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": stream,
    }


def get_inference_payload(model, provider, stream):
    """
    Generate data for inference endpoint (text_generation).
    :param model: model name.
    :param provider: provider name.
    :param stream: stream.
    :return: data.
    """
    return {
        "model": model,
        "provider": provider,
        "arguments": _partial_openai_payload(stream=stream),
    }


def get_chat_completions_payload(model, provider, stream):
    """
    Generate data for chat completions.

    :param model: model name.
    :param stream: stream.

    :return: data.
    """
    return {"model": f"{model}@{provider}", **_partial_openai_payload(stream=stream)}


def get_chat_completions_payload_fallback(model_str, stream):
    return {"model": model_str, **_partial_openai_payload(stream=stream)}


def check_in_dict_and_instance(dict, key, types):
    assert key in dict
    assert isinstance(dict.get(key), types)


def check_text_gen_response(response: Dict, object_str: str):

    response_id = response.get("id")
    assert response_id is None or isinstance(response_id, str)
    assert response.get("object") == object_str
    response_created = response.get("created")
    assert response_created is None or isinstance(response_created, int)
    # TODO: We need to add a system_fingerprint
    # assert isinstance(response.get("system_fingerprint"), str)
    assert isinstance(response.get("choices"), list)

    if object_str != "chat.completion.chunk":
        assert isinstance(response.get("usage"), dict)

    if "provider" in response:
        model, provider = response["model"], response["provider"]
    else:
        model, provider = response["model"].split("@")

    assert isinstance(model, str)
    assert isinstance(provider, str)


def check_text_gen_usage(usage: Dict):
    assert isinstance(usage, dict)
    assert isinstance(usage.get("completion_tokens"), int)
    assert isinstance(usage.get("prompt_tokens"), int)
    assert isinstance(usage.get("total_tokens"), int)


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
    message_dict = choice.get(message)
    assert isinstance(message_dict, dict)
    if message == "message":
        assert isinstance(message_dict.get("role"), str)
    # TODO: Add option for empty message if end of stream and delta
    assert isinstance(message_dict.get("content"), str)
