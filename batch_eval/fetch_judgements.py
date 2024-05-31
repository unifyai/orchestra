import json
import os

from generic_mp import process_requests
import request_handling
from judge_configs import format_no_ref, format_with_ref


def create_judge_prompt(prompt_data):
    prompt = prompt_data["prompt"]
    model_response = prompt_data["model_response"]
    if "ref_answer" in prompt_data:
        judge_prompt = format_with_ref(
            prompt=prompt, ref_ans=prompt_data["ref_answer"], model_resp=model_response
        )
    else:
        judge_prompt = format_no_ref(prompt=prompt, model_resp=model_response)
    return judge_prompt


def create_request(model_tag: str, url, headers, prompt_data: dict, model_name):
    prompt = create_judge_prompt(prompt_data)
    payload = request_handling.create_payload(model_tag=model_tag, prompt=prompt)
    return request_handling.Request(
        id_=prompt_data["id"],
        payload=payload,
        url=url,
        headers=headers,
        prompt=prompt_data["prompt"],
        response_type="judge_response",
        model_name=model_name,
    )


async def generate_judgements(
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
            req = create_request(judge_model_tag, url, headers, data, asst_model_name)
            unprocessed_prompts.append(req)

    print(f"{len(no_resp)=}")
    print(f"{len(unprocessed_prompts)=}")

    await process_requests(
        unprocessed_prompts,
        response_filename=judge_response_file,
        batch_size=batch_size,
        tries=5,
    )
