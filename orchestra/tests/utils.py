import json
import os

from starlette import status

from orchestra.tests.test_credits import test_credits

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


def assert_model(response_json):  # noqa: WPS218
    """
    Helper for testing responses based on common attributes.

    :param response_json: response json.

    :return: first choice.
    """
    assert "model" in response_json

    if "provider" in response_json:
        model, provider = (
            response_json["model"],  # noqa: WPS529
            response_json["provider"],  # noqa: WPS529
        )
    else:
        model, provider = response_json["model"].split("@")
    assert isinstance(model, str)
    assert isinstance(provider, str)

    assert "created" in response_json
    assert isinstance(response_json["created"], int)

    assert "id" in response_json
    assert isinstance(response_json["id"], str)

    assert "choices" in response_json
    assert isinstance(response_json["choices"], list)

    assert "object" in response_json
    assert isinstance(response_json["object"], str)

    first_choice = response_json["choices"][0]

    assert "finish_reason" in first_choice
    assert first_choice["finish_reason"] is None or isinstance(
        first_choice["finish_reason"],
        str,
    )

    assert "index" in first_choice
    assert isinstance(first_choice["index"], int)

    return first_choice


def check_text_completion_no_streaming(  # noqa: WPS218
    response_data,
    current_credits,
    client,
    fastapi_app,
):
    """
    Check text completion with no streaming.

    :param response_data: response data.
    :param current_credits: current credits.
    :param client: client for the app.
    :param fastapi_app: current FastAPI application.
    """
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

    final_credits = test_credits(client, fastapi_app)
    assert final_credits < current_credits


def check_text_completion_streaming(  # noqa: WPS218
    model,
    response,
    current_credits,
    client,
    fastapi_app,
):
    """
    Check text completion with streaming.

    :param model: model name.
    :param response: response.
    :param current_credits: current credits.
    :param client: client for the app.
    :param fastapi_app: current FastAPI application.
    """
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

    final_credits = test_credits(client, fastapi_app)
    assert final_credits < current_credits


def generate_data_inference_chat_completion(model, provider, stream):
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
                        "Give a long detailed response please"
                        "and explain all of his achievements"
                    ),
                },
            ],
            "temperature": 0.5,
            "max_tokens": 300,
            "stream": stream,
        },
    }


def generate_data_chat_completions(model, stream):
    """
    Generate data for chat completions.

    :param model: model name.
    :param stream: stream.

    :return: data.
    """
    return {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": "Explain who Newton was and his entire theory of"
                " gravitation. Give a long detailed response please"
                "and explain all of his achievements",
            },
        ],
        "max_tokens": 300,
        "stream": stream,
    }
