import copy
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient

from orchestra.tests.utils import HEADERS, get_chat_completions_payload

headers = copy.copy(HEADERS)
headers.pop("Content-Type", None)


async def test_logging_queries(client: AsyncClient):
    endpoint = "/v0/chat/completions"
    data = get_chat_completions_payload("llama-3-8b-chat", "aws-bedrock", stream=False)
    response = await client.post(endpoint, headers=HEADERS, json=data)
    assert response.status_code == 200

    endpoint = "/v0/queries"
    response = await client.get(endpoint, headers=HEADERS)
    assert response.status_code == 200, response.json()
    assert len(response.json()) == 1, response.json()


async def test_logging_failed_queries(client: AsyncClient):
    endpoint = "/v0/chat/completions"
    data = get_chat_completions_payload("llama-3-8b-chat", "aws-bedrock", stream=False)
    data["region"] = "us-east-2"
    response = await client.post(endpoint, headers=HEADERS, json=data)
    assert response.status_code == 400

    endpoint = "/v0/queries"
    response = await client.get(endpoint, headers=HEADERS, params={"failures": "only"})
    assert response.status_code == 200, response.json()
    assert len(response.json()) == 1, response.json()


async def test_logging_queries_NO_LOG(client: AsyncClient):
    endpoint = "/v0/chat/completions"
    data = get_chat_completions_payload("llama-3-8b-chat", "aws-bedrock", stream=False)
    data["log_query_body"] = False
    data["log_response_body"] = False

    response = await client.post(endpoint, headers=HEADERS, json=data)
    assert response.status_code == 200

    endpoint = "/v0/queries"
    response = await client.get(endpoint, headers=HEADERS)
    assert response.status_code == 200, response.json()
    resp_json = response.json()
    assert len(resp_json) == 1
    assert resp_json[0]["query_body"] == ""
    assert resp_json[0]["response_body"] == ""


async def test_queries_filter_endpoint(client: AsyncClient):
    endpoint = "/v0/chat/completions"
    data = get_chat_completions_payload("llama-3-8b-chat", "aws-bedrock", stream=False)
    response = await client.post(endpoint, headers=HEADERS, json=data)
    assert response.status_code == 200

    endpoint = "/v0/queries"
    data = {"endpoints": ["llama-3-8b-chat@aws-bedrock"]}
    response = await client.get(endpoint, headers=HEADERS, params=data)
    assert response.status_code == 200, response.json()
    assert len(response.json()) == 1, response.json()


async def test_query_timestamped(client: AsyncClient):

    st = str(datetime.now(timezone.utc))

    endpoint = "/v0/chat/completions"
    data = get_chat_completions_payload("llama-3-8b-chat", "aws-bedrock", stream=False)
    response = await client.post(endpoint, headers=HEADERS, json=data)
    assert response.status_code == 200

    endpoint = "/v0/queries"
    data = {"start_time": st}
    response = await client.get(endpoint, headers=HEADERS, params=data)
    assert response.status_code == 200, response.json()
    assert len(response.json()) == 1, response.json()

    endpoint = "/v0/queries"
    data = {"start_time": str(datetime.now(timezone.utc))}
    response = await client.get(endpoint, headers=HEADERS, params=data)
    assert response.status_code == 200, response.json()
    assert len(response.json()) == 0, response.json()


@pytest.mark.anyio
async def test_tags(client: AsyncClient):
    endpoint = "/v0/chat/completions"
    data = get_chat_completions_payload("llama-3-8b-chat", "aws-bedrock", stream=False)
    data["tags"] = ["dummy_tag"]
    response = await client.post(endpoint, headers=HEADERS, json=data)
    assert response.status_code == 200

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
    assert response.status_code == 200

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
    assert response.status_code == 200

    endpoint = "/v0/queries"
    data = {"tags": ["dummy_tag_FAKE"]}
    response = await client.get(endpoint, headers=HEADERS, params=data)
    assert response.status_code == 200, response.json()
    assert len(response.json()) == 0, response.json()


external_data = {
    "endpoint": "local_model_test@external",
    "query_body": {
        "messages": [
            {"role": "system", "content": "You are an useful assistant"},
            {"role": "user", "content": "Explain who Newton was."},
        ],
        "model": "llama-3-8b-chat@aws-bedrock",
        "max_tokens": 100,
        "temperature": 0.5,
    },
    "response_body": {
        "model": "meta.llama3-8b-instruct-v1:0",
        "created": 1725396241,
        "id": "chatcmpl-92d3b36e-7b64-4ae8-8102-9b7e3f5dd30f",
        "object": "chat.completion",
        "usage": {
            "completion_tokens": 100,
            "prompt_tokens": 44,
            "total_tokens": 144,
        },
        "choices": [
            {
                "finish_reason": "stop",
                "index": 0,
                "message": {
                    "content": "Sir Isaac Newton was an English mathematician, physicist, and astronomer who lived from 1643 to 1727.\\n\\nHe is widely recognized as one of the most influential scientists in history, and his work laid the foundation for the Scientific Revolution of the 17th century.\\n\\nNewton's most famous achievement is his theory of universal gravitation, which he presented in his groundbreaking book \"Philosophi\\u00e6 Naturalis Principia Mathematica\" in 1687.\\n\\nAccording to Newton's theory, every",
                    "role": "assistant",
                },
            },
        ],
    },
}


@pytest.mark.anyio
async def test_external_logging(client: AsyncClient):
    endpoint = "/v0/queries"
    response = await client.post(endpoint, headers=HEADERS, json=external_data)
    assert response.status_code == 200, response.json()

    endpoint = "/v0/queries"
    data = {"endpoints": ["local_model_test@external"]}
    response = await client.get(endpoint, headers=HEADERS, params=data)
    assert response.status_code == 200, response.json()
    assert len(response.json()) == 1, response.json()


@pytest.mark.anyio
async def test_repeated_external_logging(client: AsyncClient):
    endpoint = "/v0/queries"
    for _ in range(2):
        response = await client.post(endpoint, headers=HEADERS, json=external_data)
        assert response.status_code == 200, response.json()

    endpoint = "/v0/queries"
    data = {"endpoints": ["local_model_test@external"]}
    response = await client.get(endpoint, headers=HEADERS, params=data)
    assert response.status_code == 200, response.json()
    assert len(response.json()) == 2, response.json()


if __name__ == "__main__":
    pass
