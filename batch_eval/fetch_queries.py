import json
import os
from functools import partial

from generic_mp import process_requests
import request_handling


def create_request(model_tag: str, api_fn, prompt_data: dict):
    payload = request_handling.create_payload(
        model_tag=model_tag, prompt=prompt_data["prompt"]
    )
    return request_handling.Request(
        id_=prompt_data["id"],
        payload=payload,
        api_fn=api_fn,
        prompt=prompt_data["prompt"],
        response_type="model_response",
    )


async def generate_queries(prompt_file, response_file, model_tag, batch_size, api_key):
    model_name = model_tag.split("@")[0]

    print(f"Generating queries for: {model_tag}")

    url = f'{os.getenv("ORCHESTRA_BASE_URL")}/v0/chat/completions'
    headers = {"Authorization": f"Bearer {api_key}"}

    api_fnc = partial(request_handling.call_api, url=url, headers=headers)

    completed = set()
    if os.path.isfile(response_file):
        with open(response_file) as f:
            for line in f:
                data = json.loads(line)
                completed.add(data["id_"])

    unprocessed_prompts = []
    with open(prompt_file, "r") as pf:
        for ix, line in enumerate(pf):
            data = json.loads(line)
            data["id"] = data["id_"]
            if data["id"] in completed:
                continue
            req = create_request(model_tag, api_fnc, data)
            unprocessed_prompts.append(req)

    print(len(unprocessed_prompts))
    process_requests(
        unprocessed_prompts,
        response_filename=response_file,
        batch_size=batch_size,
        tries=5,
    )
