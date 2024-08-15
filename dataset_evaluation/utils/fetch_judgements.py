import json
import os
from functools import partial

from utils.generic_mp import process_requests
from utils.request_handling import Request, create_payload
from utils.judge_templates import template_with_ref
from utils.parsing_judge import ratings_from_sample

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

{"assistant_rating": RATING}

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


def create_judge_prompt(prompt_data, eval_config):
    system_prompt = eval_config.get("system_prompt", None)
    class_cfg = eval_config.get("class_cfg", None)
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
    model_endpoint: str,
    judge_endpoint: str,
    url,
    headers,
    client,
    prompt_data: dict,
    eval_config,
):
    prompt = create_judge_prompt(
        prompt_data,
        eval_config,
    )
    payload = create_payload(model_tag=judge_endpoint, prompt=prompt)
    score_fn = partial(ratings_from_sample, cfg=eval_config.get("class_config", None))
    return Request(
        id_=prompt_data["id_"],
        payload=payload,
        url=url,
        headers=headers,
        client=client,
        prompt=prompt_data["prompt"],
        response_type="judge_response",
        extra_kwargs={
            "endpoint": model_endpoint,
            "judge_endpoint": judge_endpoint,
            "model_response": prompt_data["model_response"],
        },
        score_fn=score_fn,
    )


async def generate_judgements(
    asst_response_file,
    judge_response_file,
    asst_model_tag,
    judge_model_tag,
    batch_size,
    api_key,
    client,
    eval_config,
):
    url = f"/v0/chat/completions"
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

    unprocessed_prompts = []
    with open(asst_response_file) as f:
        for ix, line in enumerate(f):
            data = json.loads(line)
            if data["id_"] in completed:
                continue
            req = create_request(
                model_endpoint=asst_model_tag,
                judge_endpoint=judge_model_tag,
                url=url,
                headers=headers,
                client=client,
                prompt_data=data,
                eval_config=eval_config,
            )
            unprocessed_prompts.append(req)

    print(f"{len(unprocessed_prompts)=}")

    await process_requests(
        unprocessed_prompts,
        response_filename=judge_response_file,
        batch_size=batch_size,
        tries=5,
    )
