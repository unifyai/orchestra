import asyncio
import json
import logging
import os
from dataclasses import dataclass

from utils.fetch_queries import generate_queries
from utils.fetch_judgements import generate_judgements
from utils.extract_score import ratings_from_sample
from utils.token_counts import count_tokens


@dataclass
class BenchmarkConfig:
    benchmark_name: str
    models_to_benchmark: list[str]
    judge_model_tag: str
    user_id: str
    user_email: str
    api_key: str


async def main(msg, data_dir):
    """msg is a json object with two fields: config and prompts
    prompts is a list of json objects of the form {"prompt", "reference_answer"}.
    """
    msg = json.loads(msg)
    cfg = msg["config"]
    cfg = BenchmarkConfig(**cfg)

    prompts = msg["prompts"]

    assert len(cfg.models_to_benchmark) > 0, "No models specified."

    # create root folder

    run_name = f"{cfg.user_id}_{cfg.benchmark_name}"
    run_save_path = os.path.join(data_dir, run_name)
    model_responses_path = os.path.join(run_save_path, "model_responses")
    model_judgements_path = os.path.join(run_save_path, "model_judgements")
    prompts_path = os.path.join(run_save_path, "prompts.jsonl")

    if not os.path.isdir(run_save_path):
        os.mkdir(run_save_path)
        os.mkdir(model_responses_path)
        os.mkdir(model_judgements_path)
        # make prompt file
        with open(prompts_path, "w") as f:
            for id_, entry in enumerate(prompts):
                entry["id_"] = id_
                f.write(json.dumps(entry) + "\n")
    else:
        with open(prompts_path) as f:
            for ix, (line, p_new) in enumerate(zip(f, prompts)):
                p_old = json.loads(line)
                p_new["id_"] = ix
                assert p_old == p_new, f"mismatch in prompts, {p_old}, {p_new}"

    def _format_model_tag(model_tag):
        return model_tag.replace("@", "___")

    def _format_judgements_file(model_tag, judge_model_tag):
        return _format_model_tag(model_tag) + "___" + _format_model_tag(judge_model_tag)

    async def process_queries(model_tag, prompts_path, model_responses_path, api_key):
        model_str = _format_model_tag(model_tag)
        await generate_queries(
            prompt_file=prompts_path,
            response_file=os.path.join(model_responses_path, f"{model_str}.jsonl"),
            model_tag=model_tag,
            batch_size=5,
            api_key=api_key,
        )

    tasks = [
        process_queries(model_tag, prompts_path, model_responses_path, cfg.api_key)
        for model_tag in cfg.models_to_benchmark
    ]
    logging.basicConfig(
        level=logging.DEBUG,
        format=f"%(asctime)s - %(levelname)s - {str(run_name)} - %(message)s",
    )
    logging.info(f"Begin getting queries")
    await asyncio.gather(*tasks)
    logging.info(f"End getting queries")

    async def process_judgements(
        model_tag,
        judge_model_tag,
        prompts_path,
        model_responses_path,
        model_judgements_path,
        api_key,
    ):
        model_str = _format_model_tag(model_tag)
        judgements_file_str = _format_judgements_file(model_tag, judge_model_tag)
        await generate_judgements(
            prompt_file=prompts_path,
            asst_response_file=os.path.join(model_responses_path, f"{model_str}.jsonl"),
            judge_response_file=os.path.join(
                model_judgements_path, f"{judgements_file_str}.jsonl"
            ),
            asst_model_tag=model_tag,
            judge_model_tag=judge_model_tag,
            batch_size=2,
            api_key=api_key,
        )

    tasks = [
        process_judgements(
            model_tag,
            cfg.judge_model_tag,
            prompts_path,
            model_responses_path,
            model_judgements_path,
            cfg.api_key,
        )
        for model_tag in cfg.models_to_benchmark
    ]

    logging.info(f"Begin getting judgements")
    await asyncio.gather(*tasks)
    logging.info(f"End getting judgements")

    # get router scores on the prompts

    # count all tokens
    # use the api for this ?
    id_model_to_tokens = count_tokens(root_dir=run_save_path)

    logging.info(f"Begin collating")
    id_to_model_to_scores = {}
    for model_tag in cfg.models_to_benchmark:
        model_str = _format_model_tag(model_tag)
        judgements_file_str = _format_judgements_file(model_tag, cfg.judge_model_tag)
        judge_response_file = os.path.join(
            model_judgements_path, f"{judgements_file_str}.jsonl"
        )
        if not os.path.exists(judge_response_file):
            logging.info(
                f"Judge response file does not exist for {judgements_file_str}"
            )
            continue

        with open(judge_response_file) as f:
            for line in f:
                data = json.loads(line)
                judge_response = data["judge_response"]
                score = ratings_from_sample(judge_response)
                if data["id_"] not in id_to_model_to_scores:
                    id_to_model_to_scores[data["id_"]] = {}
                id_to_model_to_scores[data["id_"]][model_str] = score

    # upload to the db
    #


if __name__ == "__main__":
    providers = [
        "together-ai",
        "fireworks-ai",
        "groq",
        "octoai",
        "aws-bedrock",
        "lepton-ai",
        "deepinfra",
    ]
    models = [f"llama-3-8b-chat@{p}" for p in providers]
    cfg = {
        "benchmark_name": "battle_of_the_llamas",
        "models_to_benchmark": models,
        "judge_model_tag": "gpt-4o@openai",
        "user_id": "clwq7wcn00006o7rt5nea9ktt",
        "user_email": "tje541@gmail.com",
        "api_key": os.environ["UNIFY_API_KEY"],
    }
    prompts = [{"prompt": "hello world"}]
    prompts = []
    with open("/home/tje/Downloads/11_Jun_routerbench_datasets_mtbench.jsonl") as f:
        prompts = [json.loads(l) for l in f]
    prompts = prompts[:2]
    msg_d = {"config": cfg, "prompts": prompts}
    msg_raw = json.dumps(msg_d)
    save_dir = "tmp/"
    asyncio.run(main(msg_raw, save_dir))
