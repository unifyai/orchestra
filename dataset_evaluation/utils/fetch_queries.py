import json
import os

from httpx import AsyncClient
import tiktoken

from utils.helpers import load_prompt, load_response, get_llm_response


async def send_response_to_db(prompt_id, endpoint_str, admin_key, client, response):
    url = "/v0/evaluations/upload_responses"
    HEADERS = {
        "accept": "application/json",
        "Authorization": f"Bearer {admin_key}",
        "Content-Type": "application/json",
    }
    encoding = tiktoken.get_encoding("cl100k_base")
    num_tokens = encoding.encode(response["choices"][0]["message"]["content"])

    params = {
        "prompt_id": prompt_id,
        "endpoint_str": endpoint_str,
        "response": json.dumps(response),
        "num_tokens": num_tokens,
    }
    response = await client.post(url, headers=HEADERS, params=params)
    return response.status_code


async def generate_response(
    prompt_id,
    endpoint_str,
    cfg,
    client,
    semaphore,
):
    async with semaphore:
        # check we haven't already got the response:
        response = await load_response(
            prompt_id=prompt_id,
            endpoint_str=endpoint_str,
            admin_key=cfg.admin_key,
            client=client,
        )
        if response:
            return
        # get the prompt from the db
        prompt_data = await load_prompt(prompt_id, cfg.admin_key, client)

        # run the query
        system_msg = json.loads(prompt_data.get("system_msg"))
        messages = json.loads(prompt_data.get("messages"))
        prompt_kwargs = json.loads(prompt_data.get("prompt_kwargs"))
        if system_msg:
            messages = system_msg + messages
        payload = {"model": endpoint_str, "messages": messages, **prompt_kwargs}

        url = f"/v0/chat/completions"
        headers = {"Authorization": f"Bearer {cfg.api_key}"}
        response = await get_llm_response(payload, url, headers, client)

        # upload the response
        db_upload_msg = await send_response_to_db(
            prompt_id=prompt_id,
            endpoint_str=endpoint_str,
            admin_key=cfg.admin_key,
            client=client,
            response=response,
        )
        if db_upload_msg != 200:
            raise Exception
