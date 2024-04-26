import json
import os
from functools import partial

from generic_mp import process_requests
import request_handling

from judge_configs import format_no_ref


def create_judge_prompt(prompt_data):
    prompt = prompt_data["prompt"]
    model_response = prompt_data["model_response"]
    judge_prompt = format_no_ref(prompt=prompt, model_resp=model_response)
    return judge_prompt


def create_request(model_tag: str, api_fn, prompt_data: dict, model_name):

    prompt = create_judge_prompt(prompt_data)
    payload = request_handling.create_payload(model_tag=model_tag, prompt=prompt)
    return request_handling.Request(
        id_=prompt_data["id"],
        payload=payload,
        api_fn=api_fn,
        prompt=prompt_data["prompt"],
        response_type="judge_response",
        model_name=model_name,
    )


def generate_judgements(
    prompt_file,
    asst_response_file,
    judge_response_file,
    asst_model_tag,
    judge_model_tag,
    batch_size,
    api_key,
):
    url = f'{os.getenv("ORCHESTRA_BASE_URL")}/v0/chat/completions'
    headers = {"Authorization": f"Bearer {api_key}"}

    api_fnc = partial(request_handling.call_api, url=url, headers=headers)

    asst_model_name = asst_model_tag.split("@")[0]
    judge_model_name = judge_model_tag.split("@")[0]

    print(f"Generating judgements for: {asst_model_tag}")

    completed = set()
    if os.path.isfile(judge_response_file):
        with open(judge_response_file) as f:
            for line in f:
                data = json.loads(line)
                completed.add(data["id_"])

    id_to_response = {}
    with open(asst_response_file) as f:
        for line in f:
            data = json.loads(line)
            id_to_response[data["id_"]] = data["model_response"]

    unprocessed_prompts = []
    no_resp = []
    with open(prompt_file) as f:
        for ix, line in enumerate(f):
            data = json.loads(line)
            data["row_id"] = data["id_"]
            if data["row_id"] in completed:
                continue
            if data["row_id"] not in id_to_response:
                no_resp.append(data)
                continue
            data["model_response"] = id_to_response[data["row_id"]]
            data["id"] = data["row_id"]

            if "!!!!!!!" in data["model_response"]:
                continue
            req = create_request(judge_model_tag, api_fnc, data, asst_model_name)
            unprocessed_prompts.append(req)

    print(f"{len(no_resp)=}")
    print(f"{len(unprocessed_prompts)=}")
    # assert False
    process_requests(
        unprocessed_prompts,
        response_filename=judge_response_file,
        batch_size=batch_size,
        tries=5,
    )


if __name__ == "__main__":
    import argparse
    import time

    parser = argparse.ArgumentParser()
    parser.add_argument("--asst_model_id", type=str, required=True)
    parser.add_argument("--judge_model_tag", type=str, default="gpt-4-0125-preview")
    parser.add_argument("--batch_size", type=int, required=True)
    args = parser.parse_args()
    print(args)
    time.sleep(1)

    # UNIFY_TOKEN = os.environ["UNIFY_TOKEN"]
    # url = "https://api.unify.ai/v0/chat/completions"
    # headers = {"Authorization": f"Bearer {UNIFY_TOKEN}"}

    ###
    OPENAI_TOKEN = os.environ["OPENAI_KEY"]
    url = "https://api.openai.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {OPENAI_TOKEN}"}

    api_fnc = partial(request_handling.call_api, url=url, headers=headers)

    asst_model_name = args.asst_model_id.split("@")[0]
    judge_model_tag = args.judge_model_tag
    judge_model_name = judge_model_tag.split("@")[0]

    prompt_file = f"dataset/shuffled_data_1M_openhermes.jsonl"
    asst_response_file = f"responses/{asst_model_name}.jsonl"
    judge_response_filename = f"judgements/{asst_model_name}_{judge_model_name}.jsonl"

    completed = set()
    if os.path.isfile(judge_response_filename):
        with open(judge_response_filename) as f:
            for line in f:
                data = json.loads(line)
                completed.add(data["id_"])

    id_to_response = {}
    with open(asst_response_file) as f:
        for line in f:
            data = json.loads(line)
            id_to_response[data["id_"]] = data["model_response"]

    unprocessed_prompts = []
    no_resp = []
    with open(prompt_file) as f:
        for ix, line in enumerate(f):
            if ix > 700000:
                break
            data = json.loads(line)
            data["row_id"] = data["id_"]
            if data["row_id"] in completed:
                continue
            if data["row_id"] not in id_to_response:
                no_resp.append(data)
                continue
            data["model_response"] = id_to_response[data["row_id"]]
            data["id"] = data["row_id"]
            if "!!!!!!!" in data["model_response"]:
                continue
            req = create_request(judge_model_tag, api_fnc, data, asst_model_name)
            unprocessed_prompts.append(req)
            if len(unprocessed_prompts) >= 8000:
                break

    unprocessed_prompts = unprocessed_prompts[: 8000 - len(completed)]
    print(f"{len(no_resp)=}")
    print(f"{len(unprocessed_prompts)=}")
    # assert False
    process_requests(
        unprocessed_prompts,
        response_filename=judge_response_filename,
        batch_size=args.batch_size,
        tries=5,
    )

# python3 fetch_judgements.py --asst_model_id deepseek-coder-33b-instruct --batch_size 10
