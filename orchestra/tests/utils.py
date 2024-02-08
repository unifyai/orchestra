import json
import os

from starlette import status

from orchestra.tests.test_credits import test_credits

# TODO: Check that answers with line breaks work properly
# TODO: Add logging to the tests to see the actual responses manually if needed

api_key = str(
    os.getenv(
        "AUTH_ACCOUNT_API_KEY",
    ),
)

HEADERS = {
    "accept": "application/json",
    "Authorization": f"Bearer {api_key}",
    "Content-Type": "application/json",
}


def get_inference_payload(model, provider, stream):
    """
    Generate data for inference chat completion.

    :param model: model name.
    :param provider: provider name.
    :param stream: stream.

    :return: data.
    """
    return {
        "model": model,
        "provider": provider,
        "arguments": {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "Explain who Newton was and his entire theory of gravitation. "
                        "Give a short response with line breaks within each sentence."
                    ),
                },
            ],
            "temperature": 0.5,
            "max_tokens": 100,
            "stream": stream,
        },
    }


def get_chat_completions_payload(model, provider, stream):
    """
    Generate data for chat completions.

    :param model: model name.
    :param stream: stream.

    :return: data.
    """
    return {
        "model": f"{model}@{provider}",
        "messages": [
            {
                "role": "user",
                "content": (
                    "Explain who Newton was and his entire theory of gravitation. "
                    "Give a short response with line breaks within each sentence."
                ),
            },
        ],
        "max_tokens": 100,
        "stream": stream,
    }
