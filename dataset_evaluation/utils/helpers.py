from httpx import AsyncClient

async def load_prompt(prompt_id: int, admin_key: str, client: AsyncClient):
    url = "/v0/dataset/load_prompt"
    HEADERS = {
        "accept": "application/json",
        "Authorization": f"Bearer {admin_key}",
        "Content-Type": "application/json",
    }
    params = {"prompt_id": prompt_id}
    ret = await client.get(url, params=params, headers=HEADERS)
    return ret.json()[0]

async def get_llm_response(payload, url, headers, client):
    ret = await client.post(url, json=payload, headers=headers)
    return ret.json()