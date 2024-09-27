import copy
import json
import string

from utils.helpers import (
    get_llm_response,
    load_judgement,
    load_prompt,
    load_prompt_variation,
    load_response,
)
from utils.parsing_judge import ratings_from_sample

default_cfg = [
    {"label": "excellent", "score": 1.0},
    {"label": "very_good", "score": 0.8},
    {"label": "good", "score": 0.5},
    {"label": "bad", "score": 0.0},
    # {"label": "irrelevant", "score": 0.0},
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


def get_format_kwargs(parser, data, eval_config):
    # breakpoint()
    format_kwargs = {}
    for key, val in json.loads(eval_config[parser]).items():
        format_value = ""
        item = data["prompt" if parser == "prompt_parser" else "model_response"]
        idx_chain = val[1:-1].split("][")
        for idx in idx_chain:
            if idx.lstrip("-").isdigit():
                idx = int(idx)
                if isinstance(item, list):
                    if len(item) < idx:
                        break
            else:
                # str idx
                idx = idx[1:-1]
                if isinstance(item, dict):
                    if idx not in item:
                        break
            # format_value = item[idx]
            item = item[idx]

        # for-else (if no breaks happen)
        else:
            format_value = item

        format_kwargs[key] = format_value
    return format_kwargs


def create_judge_prompt(data, eval_config):
    judge_prompt = eval_config["judge_prompt"]
    class_cfg = eval_config.get("class_cfg", None) or default_cfg

    # ig those should be be accesible through indexing
    # TODO: do this properly
    data["prompt"].pop("user_id")
    data["prompt"].pop("id")
    data["prompt"]["extra_fields"] = {
        i["field"]: i["value"] for i in data["prompt"]["extra_fields"]
    }

    prompt_parser_formatter = get_format_kwargs("prompt_parser", data, eval_config)
    response_parser_formatter = get_format_kwargs("response_parser", data, eval_config)

    # breakpoint()
    judge_prompt = copy.deepcopy(judge_prompt)
    # what goes into the system prompt
    system_prompt_placeholders = {}

    # what goes into the user prompt
    user_prompt_placeholders = {}
    formatter = string.Formatter()

    if judge_prompt["messages"][0]["role"] == "system":
        placeholders = [
            i[1]
            for i in formatter.parse(judge_prompt["messages"][0]["content"])
            if i[1] is not None
        ]
        for placeholder in placeholders:
            system_prompt_placeholders[placeholder] = prompt_parser_formatter.get(
                placeholder,
                "",
            ) or response_parser_formatter.get(placeholder, "")

        judge_prompt["messages"][0]["content"] = judge_prompt["messages"][0][
            "content"
        ].format(**system_prompt_placeholders)

        judge_prompt["messages"][0]["content"] += "\n\n" + create_judge_rubric(
            class_cfg,
        )

    placeholders = [
        i[1]
        for i in formatter.parse(judge_prompt["messages"][-1]["content"])
        if i[1] is not None
    ]
    for placeholder in placeholders:
        user_prompt_placeholders[placeholder] = prompt_parser_formatter.get(
            placeholder,
            "",
        ) or response_parser_formatter.get(placeholder, "")

    judge_prompt["messages"][-1]["content"] = judge_prompt["messages"][-1][
        "content"
    ].format(**user_prompt_placeholders)

    if not judge_prompt["messages"][0]["role"] != "system":
        judge_prompt["messages"][-1]["content"] += "\n\n" + create_judge_rubric(
            class_cfg,
        )

    return judge_prompt


def calc_score(eval_config, judgement_str):
    score = ratings_from_sample(
        sample=judgement_str,
        cfg=json.loads(eval_config.get("class_config", None)),
    )
    return score


def parse_jgmt(jgmt):
    if isinstance(jgmt, dict):
        return jgmt["choices"][0]["message"]["content"]
    elif isinstance(jgmt, str):
        return jgmt


async def send_judgements_to_db(
    prompt_id,
    prompt_variation_id,
    endpoint_str,
    judge_model_list,
    judgement_list,
    cache_hits,
    admin_key,
    client,
    cfg,
    eval_config,
):
    url = "/v0/evaluations/upload_judgements"
    HEADERS = {
        "accept": "application/json",
        "Authorization": f"Bearer {admin_key}",
        "Content-Type": "application/json",
    }

    judgement_list = [parse_jgmt(j) for j in judgement_list]
    judgement_scores = [calc_score(eval_config, j_str) for j_str in judgement_list]
    params = {
        "prompt_id": prompt_id,
        "endpoint_str": endpoint_str,
        "evaluator_id": cfg.evaluator_id,
    }

    body = {
        "judge_model_list": judge_model_list,
        "judgement_list": judgement_list,
        "judgement_scores": judgement_scores,
        "cache_hits": cache_hits,
    }

    if prompt_variation_id:
        params["prompt_variation_id"] = prompt_variation_id

    response = await client.post(url, headers=HEADERS, params=params, json=body)
    return response


async def generate_judgement(
    prompt_id,
    endpoint_str,
    cfg,
    eval_config,
    client,
    semaphore,
):
    try:
        async with semaphore:
            prompt_variation_id = None
            if cfg.default_prompt:
                response = await load_prompt_variation(
                    prompt_id=prompt_id,
                    default_prompt_id=cfg.default_prompt_id,
                    admin_key=cfg.admin_key,
                    client=client,
                )
                prompt_variation_id = response[0]["id"]

            # get the prompt from the db
            prompt_data = await load_prompt(
                prompt_id=prompt_id,
                admin_key=cfg.admin_key,
                client=client,
            )
            # get the response from the db
            # TODO: exception handling if the response isn't there for some reason
            response_data = (
                await load_response(
                    prompt_id=prompt_id,
                    prompt_variation_id=prompt_variation_id,
                    endpoint_str=endpoint_str,
                    admin_key=cfg.admin_key,
                    client=client,
                )
            )[0]

            # create the judge prompt
            prompt_data["messages"] = json.loads(prompt_data["messages"])

            # Override the system msg if available
            # TODO: This is duplicated in fetch_queries
            default_prompt_dict = {}
            if cfg.default_prompt:
                default_prompt_dict = json.loads(cfg.default_prompt)
            if default_prompt_dict:
                try:
                    # TODO: Ideally this looks for the system msgs
                    # instead of looking at the first one
                    if default_prompt_dict["messages"][0]["role"] == "system":
                        sys_prompt = default_prompt_dict["messages"][0]["content"]
                        if prompt_data["messages"][0]["role"] != "system":
                            prompt_data["message"].insert(0, sys_prompt)
                        default_prompt_dict.pop("messages")  # remove the msgs
                    else:
                        raise ValueError
                except:
                    pass

            data = {}
            data["prompt"] = prompt_data
            data["model_response"] = json.loads(response_data["response"])["choices"][0]
            judge_prompt = create_judge_prompt(data, eval_config)

            judge_to_responses = {}
            cache_hits = []
            for judge_model in eval_config["judge_models"]:
                if isinstance(judge_prompt, str):
                    judge_prompt = {
                        "messages": [
                            {"role": "user", "content": judge_prompt},
                        ],
                    }
                payload = {"model": judge_model, **judge_prompt}

                try:
                    # check we haven't already generated this one
                    judgement = await load_judgement(
                        prompt_id=prompt_id,
                        prompt_variation_id=prompt_variation_id,
                        endpoint_str=endpoint_str,
                        evaluator_id=cfg.evaluator_id,
                        judge_endpoint_str=judge_model,
                        admin_key=cfg.admin_key,
                        client=client,
                    )
                    if judgement:
                        print("cache hit")
                        cache_hits.append(judge_model)
                        judge_to_responses[judge_model] = judgement[0]
                        continue
                except Exception as e:
                    print(f"error searching judgement cache: {e}")

                url = f"/v0/chat/completions"
                headers = {"Authorization": f"Bearer {cfg.api_key}"}
                response = await get_llm_response(
                    payload=payload,
                    url=url,
                    headers=headers,
                    client=client,
                )
                judge_to_responses[judge_model] = response

            # log it in the db
            judge_model_list = list(judge_to_responses.keys())
            responses_list = list(judge_to_responses.values())

            db_upload_msg = await send_judgements_to_db(
                prompt_id=prompt_id,
                prompt_variation_id=prompt_variation_id,
                endpoint_str=endpoint_str,
                judge_model_list=judge_model_list,
                judgement_list=responses_list,
                cache_hits=cache_hits,
                admin_key=cfg.admin_key,
                client=client,
                cfg=cfg,
                eval_config=eval_config,
            )
            if db_upload_msg.status_code != 200:
                print(db_upload_msg.text)
                raise Exception

            return (True, prompt_id, prompt_variation_id)
    except Exception as e:
        print(f"general error with judgement: {e}")
        return (False, prompt_id, prompt_variation_id)
