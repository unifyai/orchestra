import asyncio
import json
import logging
import os
import smtplib
from dataclasses import dataclass
from typing import Optional
from email.message import EmailMessage

from google.cloud import secretmanager, storage
from dataset_evaluation.utils.fetch_judgements import generate_judgements
from dataset_evaluation.utils.fetch_queries import generate_queries
from dataset_evaluation.utils.parsing_judge import ratings_from_sample
from dataset_evaluation.utils.automatic_judgements import automatic_judgements


@dataclass
class BenchmarkConfig:
    dataset_name: str
    endpoint: str
    judge_models: list[str]
    user_id: str
    api_key: str
    orchestra_url: str
    system_prompt: Optional[str] = None
    class_cfg: Optional[list[dict]] = None


body = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Dataset Evaluation Completed</title>
    <style>
        /* Styling for the email */
        body {
            margin: 0;
            padding: 0;
            font-family: Arial, sans-serif;
            background-color: #ffffff;
            text-align: center;
        }
        .header {
            background-color: #00a824;
            padding: 20px 0;
        }
        .subheader {
            background-color: #00a824;
            height: 10px;
        }
        .content {
            padding: 20px;
        }
        .button {
            display: inline-block;
            background-color: #00a824; /* White background */
            color: #ffffff; /* Green text */
            font-weight: bold; /* Bold text */
            padding: 10px 20px;
            text-decoration: none;
            border-radius: 5px;
            margin-top: 20px;
        }
        .footer {
            background-color: #f3f3f5;
            padding: 20px;
            text-align: center;
        }
        .footer a {
            margin: 0 10px;
        }
        .footer img {
            width: 36px;
            height: 36px;
        }
    </style>
</head>
<body>
    <div class="subheader">
        <!-- Green bar at the top -->
    </div>
    <div class="content">
        <h2>Hello! The Dataset Evaluation has finished 🚀</h2>
        <p>The evaluation of <<ENDPOINT>> on <<DATASET>> is ready, you can check out the results in <a href="https://console.unify.ai">your console</a>.</p>
    </div>
    <div class="subheader">
        <!-- Green bar at the top -->
    </div>
    <div class="footer">
        <a href="https://github.com/unifyai/" target="_blank" rel="noreferrer">
            <img src="https://cdn.saas.unify.ai/github.png" alt="Github" />
        </a>
        <a href="https://www.youtube.com/@unifyai" target="_blank" rel="noreferrer">
            <img src="https://cdn.saas.unify.ai/youtube.png" alt="Youtube" />
        </a>
        <a href="https://discord.gg/sXyFF8tDtm" target="_blank" rel="noreferrer">
            <img src="https://cdn.saas.unify.ai/discord.png" alt="Discord" />
        </a>
        <a href="https://twitter.com/letsunifyai" target="_blank" rel="noreferrer">
            <img src="https://cdn.saas.unify.ai/twitter.png" alt="Twitter" />
        </a>
        <a href="https://unifyai.substack.com/" target="_blank" rel="noreferrer">
            <img src="https://cdn.saas.unify.ai/substack.png" alt="Substack" />
        </a>
    </div>
</body>
</html>

"""


def send_email(user_email, endpoint, dataset):
    email_server = smtplib.SMTP("smtp.gmail.com", 587)
    email_server.starttls()
    email_addr = os.getenv("EMAIL_ADDR", "auth@unify.ai")
    email_pass = os.getenv("EMAIL_PASS", "")
    if email_pass == "":
        client = secretmanager.SecretManagerServiceClient()
        name = "projects/saas-368716/secrets/EMAIL_SERVER_PASSWORD/versions/latest"
        response = client.access_secret_version(name=name)
        email_pass = response.payload.data.decode("UTF-8")
    email_server.login(email_addr, email_pass)

    msg = EmailMessage()
    msg["From"] = f"Unify <auth@unify.ai>"
    msg["To"] = user_email
    msg["Bcc"] = "guillermo@unify.ai"
    msg["Subject"] = "Your dataset evaluation is ready!"
    local_body = body
    local_body = local_body.replace("<<ENDPOINT>>", endpoint)
    local_body = local_body.replace("<<DATASET>>", dataset)
    msg.set_content(local_body, subtype="html")

    email_server.send_message(msg)
    email_server.quit()


async def evaluate_dataset(msg, data_dir):
    """msg is a json object with two fields: config and prompts
    prompts is a list of json objects of the form {"prompt", "reference_answer"}.
    """
    cfg = json.loads(msg)
    user_email = cfg.pop("user_email", None)
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
    folder = os.path.dirname(prompts_path)
    folder = os.path.join(folder, "tmp")
    os.makedirs(folder, exist_ok=True)
    tmp_prompts_path = os.path.join(folder, f"{cfg.endpoint}_tmp_prompts.jsonl")
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

    if cfg.judge_models[0] in ["multiple_choice", "number"]:
        # automatic judge
        model_str = _format_model_tag(cfg.endpoint)
        asst_response_file = os.path.join(model_responses_path, f"{model_str}.jsonl")
        automatic_judgements_file_str = _format_judgements_file(
            cfg.endpoint, cfg.judge_models[0]
        )
        judge_response_file = os.path.join(
            model_judgements_path,
            f"{automatic_judgements_file_str}.jsonl",
        )
        automatic_judgements(
            prompt_file=prompts_path,
            asst_response_file=asst_response_file,
            judge_response_file=judge_response_file,
            parse_type=cfg.judge_models[0],
        )
    else:
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
    # TODO: upload tokens

    storage_client = storage.Client()
    bucket_name = "uploaded_datasets"

    prefix = f"{cfg.user_id}/{cfg.dataset_name}/0/"
    blobs = storage_client.list_blobs(bucket_name, prefix=prefix)

    # format is {judge: {endpoint: score}}
    results = {}
    for b in blobs:
        if "judgements" in b.name:
            judge_model = (
                b.name.split("/")[-1]
                .replace("_judgements.jsonl", "")
                .replace("___", "@")
            )
            endpoint_name = b.name.split("/")[3]
            contents = b.download_as_bytes()
            contents = contents.decode("utf-8").split("\n")
            scores = []
            for entry in contents:
                if not entry:
                    continue
                entry = json.loads(entry)
                if "score" in entry:
                    scores.append(float(entry["score"]))
                else:
                    scores.append(ratings_from_sample(entry["judge_response"]))

            avg_score = sum(scores) / len(scores)
            if judge_model in results:
                results[judge_model][endpoint_name] = avg_score
            else:
                results[judge_model] = {endpoint_name: avg_score}

    with open("scores.json", "w") as f:
        json.dump(results, f)

    blob_name = f"{cfg.user_id}/{cfg.dataset_name}/0/scores.json"
    blob = storage.Client().bucket(bucket_name).blob(blob_name)
    blob.upload_from_filename("scores.json")

    # send mail
    if user_email is not None:
        send_email(user_email, cfg.endpoint, cfg.dataset_name)
        logging.info(
            f"Email sent to {user_email} for {cfg.endpoint}:{cfg.dataset_name}",
        )


if __name__ == "__main__":

    import sys

    message_raw = sys.argv[1]
    save_dir = "save_files/"
    asyncio.run(evaluate_dataset(message_raw, save_dir))
