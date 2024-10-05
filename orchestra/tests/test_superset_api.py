import os

import pytest
from httpx import AsyncClient
from openai import AsyncOpenAI


def create_oai_client(client):
    api_key = str(os.getenv("AUTH_ACCOUNT_API_KEY"))
    oai_client = AsyncOpenAI(base_url="http://test/v0", api_key=api_key)
    setattr(oai_client, "_client", client)
    return oai_client


@pytest.mark.anyio
async def test_openai_client(client: AsyncClient):

    oai_client = create_oai_client(client)
    res = await oai_client.chat.completions.create(
        model="llama-3-8b-chat@aws-bedrock",
        messages=[{"role": "user", "content": "Say hi."}],
    )
    assert hasattr(res, "choices")
    assert len(res.choices) > 0


@pytest.mark.anyio
async def test_extra_body(client: AsyncClient):

    oai_client = create_oai_client(client)

    # openai doesn't support "echo"
    with pytest.raises(Exception):
        res = await oai_client.chat.completions.create(
            model="gpt-3.5-turbo@openai",
            messages=[{"role": "user", "content": "Who made you?"}],
            extra_body={"echo": True},
        )

    # but togetherai does (but we don't actually return the echo ...)
    res = await oai_client.chat.completions.create(
        model="llama-3-8b-chat@together-ai",
        messages=[{"role": "user", "content": "Who made you?"}],
        extra_body={"echo": True},
    )
    assert hasattr(res, "choices")
    assert len(res.choices) > 0


async def test_extra_headers_anthropic(client: AsyncClient):

    oai_client = create_oai_client(client)

    # Anthropic allows this now...
    # with pytest.raises(Exception):
    #     res = await oai_client.chat.completions.create(
    #         model="claude-3.5-sonnet@anthropic",
    #         messages=[{"role": "user", "content": "What is 1+1? Answer concisely"}],
    #         max_tokens=8192,
    #     )

    # now with the added header
    res = await oai_client.chat.completions.create(
        model="claude-3.5-sonnet@anthropic",
        messages=[{"role": "user", "content": "What is 1+1? Answer concisely"}],
        max_tokens=8192,  # The extra header makes this sequence length possible
        extra_body={
            "extra_headers": {"anthropic-beta": "max-tokens-3-5-sonnet-2024-07-15"},
        },
    )

    assert hasattr(res, "choices")
    assert len(res.choices) > 0
