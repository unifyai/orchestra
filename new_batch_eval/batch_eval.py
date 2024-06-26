import json
import logging
import os


logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(run_name)s - %(message)s",
)


@dataclass
class BenchmarkConfig:
    benchmark_name: str
    models_to_benchmark: list[str]
    judge_model: str
    user_id: str
    user_email: str
    api_key: str


def main(msg, data_dir):
    """msg is a json object with two fields: config and prompts
    prompts is a list of json objects of the form {"prompt", "reference_answer"}.
    """
    msg = json.loads(msg)
    cfg = msg["config"]
    cfg = BenchmarkConfig(**cfg)

    prompts = msg["prompts"]

    assert len(cfg.models_to_benchmark) > 0, "No models specified."

    # create root folder

    run_name = f"{user_id}_{benchmark_name}"
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
            for entry in prompts:
                f.write(json.dumps(entry) + "\n")
    else:
        with open(prompts_path) as f:
            for line, p_new in zip(f, prompts):
                p_old = json.loads(line)
                assert (
                    p_old == p_new
                ), "mismatch in prompts, but running with repeated name"

    def _format_model_tag(model_tag):
        return model_tag.replace("@", "___")

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
    log_info = {"run_name": str(run_name)}
    logging.info(f"Begin getting queries", extra=log_info)
    await asyncio.gather(*tasks)
    logging.info(f"End getting queries", extra=log_info)

    async def process_judgements(
        model_tag,
        judge_model_tag,
        prompts_path,
        model_responses_path,
        model_judgements_path,
        api_key,
    ):
        model_str = _format_model_tag(model_tag)
        await generate_judgements(
            prompt_file=prompts_path,
            asst_response_file=os.path.join(model_responses_path, f"{model_str}.jsonl"),
            judge_response_file=os.path.join(
                model_judgements_path, f"{model_name}.jsonl"
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

    logging.info(f"Begin getting judgements", extra=log_info)
    await asyncio.gather(*tasks)
    logging.info(f"End getting judgements", extra=log_info)
    
    # get router scores on the prompts

    # count all tokens
    # use the api for this ??

    # upload
