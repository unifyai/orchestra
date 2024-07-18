import asyncio
import json
import logging
import os
from dataclasses import dataclass

from google.cloud import storage
from utils.fetch_judgements import generate_judgements
from utils.fetch_queries import generate_queries


@dataclass
class BenchmarkConfig:
    dataset_name: str
    endpoint: str
    judge_models: list[str]
    user_id: str
    api_key: str
    orchestra_url: str
    system_prompt: str
    class_cfg: str


async def main(msg, data_dir):
    """msg is a json object with two fields: config and prompts
    prompts is a list of json objects of the form {"prompt", "reference_answer"}.
    """
    msg = json.loads(msg)
    cfg = msg["config"]
    cfg["class_cfg"] = json.loads(cfg["class_cfg"])
    cfg = BenchmarkConfig(**cfg)

    # create root folder
    run_save_path = os.path.join(data_dir, cfg.user_id, cfg.dataset_name)

    os.makedirs(run_save_path, exist_ok=True)

    model_responses_path = os.path.join(run_save_path, "model_responses")
    model_judgements_path = os.path.join(run_save_path, "model_judgements")
    prompts_path = os.path.join(run_save_path, "prompts.jsonl")

    # load prompts

    bucket_name = "uploaded_datasets"
    blob_name = f"{cfg.user_id}/{cfg.dataset_name}/0/dataset.jsonl"
    blob = storage.Client().bucket(bucket_name).blob(blob_name)
    tmp_prompts_path = prompts_path.replace("prompts.jsonl", "tmp_prompts.jsonl")
    blob.download_to_filename(tmp_prompts_path)
    with open(tmp_prompts_path) as f:
        prompts = [json.loads(l) for l in f]

    os.makedirs(model_responses_path, exist_ok=True)
    os.makedirs(model_judgements_path, exist_ok=True)
    if not os.path.isfile(prompts_path):
        # make prompt file
        with open(prompts_path, "w") as f:
            for id_, entry in enumerate(prompts):
                entry["id_"] = id_
                f.write(json.dumps(entry) + "\n")

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
            orchestra_url=cfg.orchestra_url,
        )

    tasks = [
        process_queries(cfg.endpoint, prompts_path, model_responses_path, cfg.api_key),
    ]
    logging.basicConfig(
        level=logging.DEBUG,
        format=f"%(asctime)s - %(levelname)s - {cfg.dataset_name} - %(message)s",
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
        system_prompt,
        class_cfg,
    ):
        model_str = _format_model_tag(model_tag)
        judgements_file_str = _format_judgements_file(model_tag, judge_model_tag)
        await generate_judgements(
            prompt_file=prompts_path,
            asst_response_file=os.path.join(model_responses_path, f"{model_str}.jsonl"),
            judge_response_file=os.path.join(
                model_judgements_path,
                f"{judgements_file_str}.jsonl",
            ),
            asst_model_tag=model_tag,
            judge_model_tag=judge_model_tag,
            batch_size=2,
            api_key=api_key,
            orchestra_url=cfg.orchestra_url,
            system_prompt=system_prompt,
            class_cfg=class_cfg,
        )

    tasks = [
        process_judgements(
            cfg.endpoint,
            judge_tag,
            prompts_path,
            model_responses_path,
            model_judgements_path,
            cfg.api_key,
            cfg.system_prompt,
            cfg.class_cfg,
        )
        for judge_tag in cfg.judge_models
    ]

    logging.info(f"Begin getting judgements")
    await asyncio.gather(*tasks)
    logging.info(f"End getting judgements")

    # get router scores on the prompts

    # count all tokens
    # use the api for this ?
    # id_model_to_tokens = count_tokens(root_dir=run_save_path)

    # logging.info(f"Begin collating")
    # id_to_model_to_scores = {}
    # for model_tag in cfg.models_to_benchmark:
    #    model_str = _format_model_tag(model_tag)
    #    judgements_file_str = _format_judgements_file(model_tag, cfg.judge_model_tag)
    #    judge_response_file = os.path.join(
    #        model_judgements_path, f"{judgements_file_str}.jsonl"
    #    )
    #    if not os.path.exists(judge_response_file):
    #        logging.info(
    #            f"Judge response file does not exist for {judgements_file_str}"
    #        )
    #        continue

    #    with open(judge_response_file) as f:
    #        for line in f:
    #            data = json.loads(line)
    #            judge_response = data["judge_response"]
    #            score = ratings_from_sample(judge_response)
    #            if data["id_"] not in id_to_model_to_scores:
    #                id_to_model_to_scores[data["id_"]] = {}
    #            id_to_model_to_scores[data["id_"]][model_str] = score

    # upload to cloud storage buckets
    #
    # upload responses
    blob_name = f"{cfg.user_id}/{cfg.dataset_name}/0/{cfg.endpoint}/responses.jsonl"
    blob = storage.Client().bucket(bucket_name).blob(blob_name)
    blob.upload_from_filename(
        os.path.join(model_responses_path, _format_model_tag(cfg.endpoint) + ".jsonl"),
    )
    # upload judgements
    for judge_tag in cfg.judge_models:
        fmtd_judge_tag = _format_model_tag(judge_tag)
        blob_name = f"{cfg.user_id}/{cfg.dataset_name}/0/{cfg.endpoint}/{fmtd_judge_tag}_judgements.jsonl"
        blob = storage.Client().bucket(bucket_name).blob(blob_name)
        blob.upload_from_filename(
            os.path.join(
                model_judgements_path,
                _format_judgements_file(cfg.endpoint, judge_tag) + ".jsonl",
            ),
        )
    # upload tokens
    # TODO


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--user_id", required=True)
    parser.add_argument("--api_key", required=True)
    parser.add_argument("--orchestra_url", required=True)
    parser.add_argument("--dataset_name", required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--judge_models", type=str, required=True)
    parser.add_argument("--system_prompt", type=str, default="")
    parser.add_argument("--class_cfg", type=str)
    args = parser.parse_args()

    judge_model_list = args.judge_models.split(",")
    if args.class_cfg:
        class_cfg = json.loads(args.class_cfg)
    else:
        class_cfg = None
    cfg = {
        "dataset_name": args.dataset_name,
        "endpoint": args.endpoint,
        "judge_models": judge_model_list,
        "user_id": args.user_id,
        "api_key": args.api_key,
        "orchestra_url": args.orchestra_url,
        "system_prompt": args.system_prompt,
        "class_cfg": class_cfg,
    }

    msg_d = {"config": cfg}
    msg_raw = json.dumps(msg_d)
    save_dir = "save_files/"
    asyncio.run(main(msg_raw, save_dir))
