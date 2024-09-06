import asyncio
import json
import logging
import os
import smtplib
import sys
from dataclasses import dataclass
from email.message import EmailMessage

import tiktoken
from google.cloud import secretmanager, storage
from httpx import AsyncClient, Limits
from refresh_scores import refresh_scores_for_dataset
from utils.automatic_judgements import automatic_judgements
from utils.fetch_judgements import generate_judgements
from utils.fetch_queries import generate_queries


@dataclass
class BenchmarkConfig:
    action: str
    dataset: str
    endpoint: str
    eval_id: str
    user_id: str
    api_key: str
    orchestra_url: str


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


async def evaluate_dataset(msg, data_dir, shared_volume="", client=None):
    """msg is a json object with two fields: config and prompts
    prompts is a list of json objects of the form {"prompt", "reference_answer"}.
    """
    cfg = json.loads(msg)
    user_email = cfg.pop("user_email", None)
    cfg = BenchmarkConfig(**cfg)

    # create root folder
    run_save_path = os.path.join(shared_volume, data_dir, cfg.user_id, cfg.dataset)

    os.makedirs(run_save_path, exist_ok=True)

    model_responses_path = os.path.join(run_save_path, "model_responses")
    model_judgements_path = os.path.join(run_save_path, "model_judgements")
    prompts_path = os.path.join(run_save_path, "prompts.jsonl")

    # load prompts

    bucket_name = "uploaded_datasets"
    blob_name = os.path.join(cfg.user_id, cfg.dataset, "0", "dataset.jsonl")
    folder = os.path.dirname(prompts_path)
    folder = os.path.join(folder, "tmp")
    os.makedirs(folder, exist_ok=True)
    tmp_prompts_path = os.path.join(folder, f"{cfg.endpoint}_tmp_prompts.jsonl")
    if os.environ.get("ON_PREM"):
        blob_name = os.path.join(shared_volume, bucket_name, blob_name)
        os.makedirs(os.sep.join(blob_name.split(os.sep)[:-1]), exist_ok=True)
        with open(tmp_prompts_path, "w") as f:
            f.write(open(blob_name).read())
    else:
        blob = storage.Client().bucket(bucket_name).blob(blob_name)
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

    # load eval_config
    bucket_name = "uploaded_datasets"
    blob_name = os.path.join(cfg.user_id, "evaluation_configs", f"{cfg.eval_id}.config")
    if os.environ.get("ON_PREM"):
        with open(os.path.join(shared_volume, bucket_name, blob_name), "rb") as f:
            eval_config = json.loads(f.read().decode("utf-8"))
    else:
        blob = storage.Client().bucket(bucket_name).blob(blob_name)
        eval_config = json.loads(blob.download_as_bytes().decode("utf-8"))

    if client is None:
        limits = Limits(
            max_keepalive_connections=None,
            max_connections=None,
            keepalive_expiry=30,
        )
        client = AsyncClient(base_url=cfg.orchestra_url, limits=limits, timeout=60)

    def _format_model_tag(model_tag):
        return model_tag.replace("@", "___")

    def _format_judgements_file(model_tag, judge_model_tag, eval_id):
        return (
            _format_model_tag(model_tag)
            + "___"
            + eval_id
            + "___"
            + _format_model_tag(judge_model_tag)
        )

    async def process_queries(
        endpoint,
        prompts_path,
        model_responses_path,
        api_key,
        client,
    ):
        model_str = _format_model_tag(endpoint)
        response_blob_name = os.path.join(
            cfg.user_id,
            cfg.dataset,
            "0",
            endpoint,
            "responses.jsonl",
        )
        progress_blob_name = os.path.join(
            cfg.user_id,
            cfg.dataset,
            "0",
            endpoint,
            "progress.log",
        )
        await generate_queries(
            prompt_file=prompts_path,
            response_file=os.path.join(model_responses_path, f"{model_str}.jsonl"),
            endpoint=endpoint,
            batch_size=5,
            api_key=api_key,
            client=client,
            gcp_config={
                "bucket_name": "uploaded_datasets",
                "response_blob_name": response_blob_name,
                "progress_blob_name": progress_blob_name,
                "num_prompts": len(prompts),
            },
        )

    tasks = [
        process_queries(
            endpoint=cfg.endpoint,
            prompts_path=prompts_path,
            model_responses_path=model_responses_path,
            api_key=cfg.api_key,
            client=client,
        ),
    ]
    logging.basicConfig(
        level=logging.DEBUG,
        format=f"%(asctime)s - %(levelname)s - {cfg.dataset} - %(message)s",
    )
    logging.info(f"Begin getting queries")
    await asyncio.gather(*tasks)
    logging.info(f"End getting queries")

    def create_judgement_blob_filename(endpoint, eval_id, judge_tag):
        blob_name = os.path.join(
            cfg.user_id,
            cfg.dataset,
            "0",
            endpoint,
            f"{eval_id}",
            f"{judge_tag.replace('@', '___')}_judged.jsonl",
        )
        return blob_name

    async def process_judgements(
        endpoint,
        judge_model_tag,
        prompts_path,
        model_responses_path,
        model_judgements_path,
        api_key,
        client,
        eval_config,
    ):
        model_str = _format_model_tag(endpoint)
        judgements_file_str = _format_judgements_file(
            endpoint,
            judge_model_tag,
            cfg.eval_id,
        )

        response_blob_name = create_judgement_blob_filename(
            endpoint,
            cfg.eval_id,
            judge_model_tag,
        )
        progress_blob_name = response_blob_name.replace(
            "_judged.jsonl",
            "_progress.log",
        )
        await generate_judgements(
            asst_response_file=os.path.join(model_responses_path, f"{model_str}.jsonl"),
            judge_response_file=os.path.join(
                model_judgements_path,
                f"{judgements_file_str}.jsonl",
            ),
            asst_model_tag=endpoint,
            judge_model_tag=judge_model_tag,
            batch_size=2,
            api_key=api_key,
            client=client,
            eval_config=eval_config,
            gcp_config={
                "bucket_name": "uploaded_datasets",
                "response_blob_name": response_blob_name,
                "progress_blob_name": progress_blob_name,
                "num_prompts": len(prompts),
            },
        )

    judge_models = eval_config["judge_models"]
    if isinstance(judge_models, str):
        judge_models = [
            judge_models,
        ]
    if judge_models is None:
        judge_models = ["claude-3.5-sonnet@aws-bedrock"]

    if judge_models[0] in ["multiple_choice", "number"]:
        # automatic judge
        model_str = _format_model_tag(cfg.endpoint)
        asst_response_file = os.path.join(model_responses_path, f"{model_str}.jsonl")
        automatic_judgements_file_str = _format_judgements_file(
            cfg.endpoint,
            cfg.judge_models[0],
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
                client,
                eval_config,
            )
            for judge_tag in judge_models
        ]

        logging.info(f"Begin getting judgements")
        await asyncio.gather(*tasks)
        logging.info(f"End getting judgements")

    print("Done collecting data")
    # upload to cloud storage buckets

    ## upload responses
    blob_name = os.path.join(
        cfg.user_id,
        cfg.dataset,
        "0",
        cfg.endpoint,
        "responses.jsonl",
    )
    model_responses_formatted_path = os.path.join(
        model_responses_path,
        _format_model_tag(cfg.endpoint) + ".jsonl",
    )
    if os.environ.get("ON_PREM"):
        blob_name = os.path.join(shared_volume, bucket_name, blob_name)
        os.makedirs(os.sep.join(blob_name.split(os.sep)[:-1]), exist_ok=True)
        with open(blob_name, "w") as f:
            f.write(open(model_responses_formatted_path).read())
    else:
        blob = storage.Client().bucket(bucket_name).blob(blob_name)
        blob.upload_from_filename(model_responses_formatted_path)

    ## upload judgements

    for judge_tag in judge_models:
        fmtd_judge_tag = _format_model_tag(judge_tag)
        blob_name = create_judgement_blob_filename(cfg.endpoint, cfg.eval_id, judge_tag)

        model_judgements_formatted_path = os.path.join(
            model_judgements_path,
            _format_judgements_file(cfg.endpoint, judge_tag, cfg.eval_id) + ".jsonl",
        )
        if os.environ.get("ON_PREM"):
            blob_name = os.path.join(shared_volume, bucket_name, blob_name)
            os.makedirs(os.sep.join(blob_name.split(os.sep)[:-1]), exist_ok=True)
            with open(blob_name, "w") as f:
                f.write(open(model_judgements_formatted_path).read())
        else:
            blob = storage.Client().bucket(bucket_name).blob(blob_name)
            blob.upload_from_filename(model_judgements_formatted_path)

    # upload num of tokens in responses
    model_str = _format_model_tag(cfg.endpoint)
    asst_response_file = os.path.join(model_responses_path, f"{model_str}.jsonl")
    encoding = tiktoken.get_encoding("cl100k_base")
    num_output_tokens = 0
    with open(asst_response_file) as f:
        for line in f:
            data = json.loads(line)
            model_response = data["model_response"]
            num_output_tokens += len(encoding.encode(model_response))
    blob_name = (
        f"{cfg.user_id}/{cfg.dataset}/0/{cfg.endpoint}/num_tokens_in_responses.json"
    )
    string = json.dumps({"num_tokens": num_output_tokens})
    if os.environ.get("ON_PREM"):
        blob_name = os.path.join(shared_volume, bucket_name, blob_name)
        os.makedirs(os.sep.join(blob_name.split(os.sep)[:-1]), exist_ok=True)
        with open(blob_name, "w") as f:
            f.write(string)
    else:
        blob = storage.Client().bucket(bucket_name).blob(blob_name)
        blob.upload_from_string(string, content_type="application/json")

    print("refreshing scores")
    refresh_scores_for_dataset(cfg.user_id, cfg.dataset)
    print("done refreshing scores")

    # send mail
    if not os.environ.get("ON_PREM") and user_email is not None:
        send_email(user_email, cfg.endpoint, cfg.dataset)
        logging.info(
            f"Email sent to {user_email} for {cfg.endpoint}:{cfg.dataset}",
        )


if __name__ == "__main__":
    shared_volume = os.environ.get("SHARED_VOLUME", "")
    message_raw = sys.argv[1]
    save_dir = "save_files/"
    asyncio.run(evaluate_dataset(message_raw, save_dir, shared_volume))
