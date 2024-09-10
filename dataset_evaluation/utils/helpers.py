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


async def load_response(
    prompt_id: int, endpoint_str: str, admin_key: str, client: AsyncClient
):
    url = "/v0/dataset/load_response"
    HEADERS = {
        "accept": "application/json",
        "Authorization": f"Bearer {admin_key}",
        "Content-Type": "application/json",
    }
    params = {"prompt_id": prompt_id, "endpoint_str": endpoint_str}
    ret = await client.get(url, params=params, headers=HEADERS)
    return ret.json()


async def load_judgement(
    prompt_id: int,
    endpoint_str: str,
    evaluator_id: str,
    admin_key: str,
    client: AsyncClient,
):
    url = "/v0/dataset/load_judgement"
    HEADERS = {
        "accept": "application/json",
        "Authorization": f"Bearer {admin_key}",
        "Content-Type": "application/json",
    }
    params = {
        "prompt_id": prompt_id,
        "endpoint_str": endpoint_str,
        "evaluator_id": evaluator_id,
    }
    ret = await client.get(url, params=params, headers=HEADERS)
    return ret.json()


async def get_llm_response(payload, url, headers, client):
    ret = await client.post(url, json=payload, headers=headers)
    return ret.json()
