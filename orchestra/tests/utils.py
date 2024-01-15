HEADERS = {
    "accept": "application/json",
    "Authorization": "Bearer mulv3oHXCvkUsodxgNgUbJJdbcu4XbP5NDEa4xk3wf8=",
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
