import json
import os
from functools import partial

from httpx import AsyncClient

from utils.judge_templates import template_with_ref
from utils.parsing_judge import ratings_from_sample
from utils.helpers import load_prompt, get_llm_response

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
    return ret.json()[0]


async def calc_score(eval_config, judgement_str):
    score = ratings_from_sample(
        sample=judgement_str, cfg=json.loads(eval_config.get("class_config", None))
    )
    return score


async def send_judgement_to_db(
    prompt_id, endpoint_str, judge_str, admin_key, client, judgement, cfg, eval_config
):
    url = "/v0/evaluations/upload_judgements"
    HEADERS = {
        "accept": "application/json",
        "Authorization": f"Bearer {admin_key}",
        "Content-Type": "application/json",
    }
    judgement_str = judgement["choices"][0]["message"]["content"]
    score = await calc_score(eval_config, judgement_str)
    params = {
        "prompt_id": prompt_id,
        "endpoint_str": endpoint_str,
        "evaluator_id": cfg.evaluator_id,
        "judge_endpoint_str": judge_str,
        "judgement": judgement_str,
        "score": score,
    }
    response = await client.post(url, headers=HEADERS, params=params)
    print(response.json())
    return response.status_code


async def generate_judgement(
    prompt_id,
    endpoint_str,
    cfg,
    eval_config,
    client,
    semaphore,
):
    async with semaphore:
        # get the prompt from the db
        prompt_data = await load_prompt(prompt_id, cfg.admin_key, client)
        # get the response from the db
        response_data = await load_response(
            prompt_id, endpoint_str, cfg.admin_key, client
        )

        # create the judge prompt
        prompt = json.loads(prompt_data["messages"])[0]["content"]
        sys_prompt = json.loads(prompt_data["system_msg"])
        if sys_prompt:
            prompt = sys_prompt + prompt
        data = {}
        data["prompt"] = prompt
        data["ref_answer"] = prompt_data["ref_answer"]
        data["model_response"] = json.loads(response_data["response"])["choices"][0][
            "message"
        ]["content"]
        judge_prompt = create_judge_prompt(data, eval_config)
        messages = [
            {"role": "user", "content": judge_prompt},
        ]

        # get the response to the judge prompt

        # TODO: what if more than one judge
        judge_model = eval_config["judge_models"][0]
        payload = {"model": judge_model, "messages": messages, "temperature": 0.3}

        url = f"/v0/chat/completions"
        headers = {"Authorization": f"Bearer {cfg.api_key}"}
        response = await get_llm_response(
            payload=payload, url=url, headers=headers, client=client
        )

        # log it in the db
        db_upload_msg = await send_judgement_to_db(
            prompt_id=prompt_id,
            endpoint_str=endpoint_str,
            judge_str=judge_model,
            admin_key=cfg.admin_key,
            client=client,
            judgement=response,
            cfg=cfg,
            eval_config=eval_config,
        )
        if db_upload_msg != 200:
            raise Exception
