import json
import os

from utils.generic_mp import process_requests
from utils.request_handling import Request, create_payload


def create_request(model_tag: str, url, headers, prompt_data):
    payload = create_payload(model_tag=model_tag, prompt=prompt_data["prompt"])
    return Request(
        id_=prompt_data["id"],
        payload=payload,
        url=url,
        headers=headers,
        prompt=prompt_data["prompt"],
        response_type="model_response",
    )


async def generate_queries(
    prompt_file, response_file, model_tag, batch_size, api_key, base_url
):
    model_name = model_tag.split("@")[0]

    print(f"Generating queries for: {model_tag}")

    url = f"{base_url}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}"}

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
            req = create_request(model_tag, url, headers, data)
            unprocessed_prompts.append(req)

    print(len(unprocessed_prompts))
    await process_requests(
        unprocessed_prompts,
        response_filename=response_file,
        batch_size=batch_size,
        tries=5,
    )
