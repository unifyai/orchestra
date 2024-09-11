import json

from utils.helpers import (
    get_llm_response,
    load_prompt,
    load_prompt_variation,
    load_response,
    store_prompt_variation,
)


async def send_response_to_db(prompt_id, endpoint_str, admin_key, client, response):
    url = "/v0/evaluations/upload_responses"
    HEADERS = {
        "accept": "application/json",
        "Authorization": f"Bearer {admin_key}",
        "Content-Type": "application/json",
    }
    num_tokens = response["usage"]["completion_tokens"]

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
    try:
        async with semaphore:
            response = await load_prompt_variation(
                prompt_id=prompt_id,
                default_prompt_id=cfg.default_prompt_id,
                admin_key=cfg.admin_key,
                client=client,
            )
            if not response:
                response = await store_prompt_variation(
                    prompt_id=prompt_id,
                    default_prompt_id=cfg.default_prompt_id,
                    admin_key=cfg.admin_key,
                    client=client,
                )
            prompt_variation_id = response["id"]

            # check we haven't already got the response:
            response = await load_response(
                prompt_id=prompt_id,
                prompt_variation_id=prompt_variation_id,
                endpoint_str=endpoint_str,
                admin_key=cfg.admin_key,
                client=client,
            )
            if response:
                return (True, prompt_id, prompt_variation_id)
            # get the prompt from the db
            prompt_data = await load_prompt(prompt_id, cfg.admin_key, client)

            default_prompt_dict = {}
            if cfg.default_prompt:
                default_prompt_dict = json.loads(cfg.default_prompt)

            # run the query
            system_msg = json.loads(prompt_data.get("system_msg"))

            # Override the system msg if available
            if default_prompt_dict:
                try:
                    # TODO: Ideally this looks for the system msgs
                    # instead of looking at the first one
                    if default_prompt_dict["messages"][0]["role"] == "system":
                        system_msg = [
                            default_prompt_dict["messages"][0],
                        ]
                        default_prompt_dict.pop("messages")  # remove the msgs
                    else:
                        raise ValueError
                except:
                    pass

            messages = json.loads(prompt_data.get("messages"))
            prompt_kwargs = json.loads(prompt_data.get("prompt_kwargs"))

            # Override the prompt kwargs
            prompt_kwargs.update(default_prompt_dict)

            if system_msg:
                messages = system_msg + messages
            payload = {"model": endpoint_str, "messages": messages, **prompt_kwargs}

            url = f"/v0/chat/completions"
            headers = {"Authorization": f"Bearer {cfg.api_key}"}
            response = await get_llm_response(payload, url, headers, client)

            # upload the response
            db_upload_msg = await send_response_to_db(
                prompt_id=prompt_id,
                prompt_variation_id=prompt_variation_id,
                endpoint_str=endpoint_str,
                admin_key=cfg.admin_key,
                client=client,
                response=response,
            )
            if db_upload_msg != 200:
                raise Exception
            return (True, prompt_id, prompt_variation_id)
    except:
        return (False, prompt_id, prompt_variation_id)
