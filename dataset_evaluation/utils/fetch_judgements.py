import json
import os

from dataset_evaluation.utils.generic_mp import process_requests
from dataset_evaluation.utils.request_handling import Request, create_payload
from dataset_evaluation.utils.judge_templates import template_with_ref

default_cfg = [
    {"label": "excellent", "score": 1.0},
    {"label": "very_good", "score": 0.8},
    {"label": "good", "score": 0.5},
    {"label": "bad", "score": 0.0},
    {"label": "irrelevant", "score": 0.0},
]


def create_judge_rubric(cfg):
    prompt = "First provide your explanation, then write down your final rating according to the following guidelines:"
    for class_head in cfg:
        head_str = f"""\n\t - "{class_head["label"]}" """
        if "description" in class_head:
            head_str += f""": {class_head["description"]}"""
        prompt += head_str

    prompt += """\nAfter that, you must output your final verdict in JSON by **strictly** following this format:

{"assistant_rating": [[RATING]]}

Do not output anything else after your final verdict, but make sure you do give a verdict, that's the most important part!"""
    return prompt


def format_q(prompt_data):
    s_to_attr = {
        "User Question": "prompt",
        "Reference Answer": "ref_answer",
        "Assistant's Answer": "model_response",
    }

    ret = ""
    for s, attr in s_to_attr.items():
        if attr in prompt_data:
            ret += f"""\n[The start of {s}]\n{prompt_data[attr]}\n[The end of {s}]"""

    return ret


def create_judge_prompt(prompt_data, system_prompt, class_cfg):
    if system_prompt:
        instructions = system_prompt
    else:
        instructions = template_with_ref
    if class_cfg:
        judge_rubric = create_judge_rubric(class_cfg)
    else:
        judge_rubric = create_judge_rubric(default_cfg)
    formatted_q = format_q(prompt_data)

    final_prompt = instructions + judge_rubric + formatted_q

    return final_prompt


def create_request(
    model_tag: str,
    url,
    headers,
    prompt_data: dict,
    model_name,
    system_prompt,
    class_cfg,
):
    prompt = create_judge_prompt(prompt_data, system_prompt, class_cfg)
    payload = create_payload(model_tag=model_tag, prompt=prompt)
    return Request(
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
    orchestra_url,
    system_prompt,
    class_cfg,
):
    url = f"{orchestra_url}/v0/chat/completions"
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
            req = create_request(
                judge_model_tag,
                url,
                headers,
                data,
                asst_model_name,
                system_prompt,
                class_cfg,
            )
            unprocessed_prompts.append(req)

    print(f"{len(no_resp)=}")
    print(f"{len(unprocessed_prompts)=}")

    await process_requests(
        unprocessed_prompts,
        response_filename=judge_response_file,
        batch_size=batch_size,
        tries=5,
    )
