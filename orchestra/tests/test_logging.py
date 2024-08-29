import copy
from httpx import AsyncClient

from orchestra.tests.utils import HEADERS

headers = copy.copy(HEADERS)
headers.pop("Content-Type", None)


async def test_prompt_history(client: AsyncClient):
    url = "/v0/prompt_history"
    params = {"tag": None}
    response = await client.get(url, params=params, headers=headers)
    assert response.status_code == 200, response.json()
    assert isinstance(response.json(), list) and len(response.json()) == 0
    payload = {
        "model": "llama-3-8b-chat@aws-bedrock",
        "messages": [{"role": "user", "content": "Say hello."}],
    }
    response = await client.post("v0/chat/completions", json=payload, headers=headers)
    assert response.status_code == 200, response.json()
    url = "/v0/prompt_history"
    params = {"tag": None}
    response = await client.get(url, params=params, headers=headers)
    assert response.status_code == 200, response.json()
    assert isinstance(response.json(), list) and len(response.json()) > 0
