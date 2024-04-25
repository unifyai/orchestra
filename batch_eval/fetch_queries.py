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


def generate_queries(prompt_file, response_file, model_tag, batch_size, api_key):
    model_name = model_tag.split("@")[0]

    url = "https://api.unify.ai/v0/chat/completions"
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


if __name__ == "__main__":
    import argparse
    import time

    parser = argparse.ArgumentParser()
    parser.add_argument("--model_tag", type=str, required=True)
    parser.add_argument("--batch_size", type=int, required=True)
    parser.add_argument("--start_id", type=int, required=True)
    parser.add_argument("--num", type=int, default=0)
    args = parser.parse_args()
    print(args)
    time.sleep(1)

    model_tag = args.model_tag
    model_name = model_tag.split("@")[0]

    if args.model_tag.startswith("gpt"):
        OPENAI_TOKEN = os.environ["OPENAI_KEY"]
        url = "https://api.openai.com/v1/chat/completions"
        headers = {"Authorization": f"Bearer {OPENAI_TOKEN}"}
        model_tag = model_name
    else:
        UNIFY_TOKEN = os.environ["UNIFY_TOKEN"]
        url = "https://api.unify.ai/v0/chat/completions"
        headers = {"Authorization": f"Bearer {UNIFY_TOKEN}"}

    api_fnc = partial(request_handling.call_api, url=url, headers=headers)

    prompt_file = f"dataset/shuffled_data_1M_openhermes.jsonl"
    response_filename = f"responses/{model_name}.jsonl"

    completed = set()
    if os.path.isfile(response_filename):
        with open(response_filename) as f:
            for line in f:
                data = json.loads(line)
                completed.add(data["id_"])

    unprocessed_prompts = []
    with open(prompt_file, "r") as pf:
        for ix, line in enumerate(pf):
            if ix < args.start_id:
                continue
            if ix >= args.start_id + args.num:
                break
            data = json.loads(line)
            data["id"] = data["id_"]
            if data["id"] in completed:
                continue
            req = create_request(model_tag, api_fnc, data)
            unprocessed_prompts.append(req)

    print(len(unprocessed_prompts))
    # assert False
    process_requests(
        unprocessed_prompts,
        response_filename=response_filename,
        batch_size=args.batch_size,
        tries=5,
    )


## python3 fetch_queries.py --model_tag
