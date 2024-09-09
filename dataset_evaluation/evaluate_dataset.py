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
    evaluator: str
    evaluator_id: str
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


async def fetch_evaluator_config(client, cfg):
    url = cfg.orchestra_url + "/v0/evaluator"
    HEADERS = {
        "accept": "application/json",
        "Authorization": f"Bearer {cfg.api_key}",
        "Content-Type": "application/json",
    }
    params = {"name": cfg.evaluator}
    response = await client.get(url, headers=HEADERS, params=params)
    eval_cfg = response.json()
    eval_cfg["judge_models"] = json.loads(eval_cfg["judge_models"])
    return eval_cfg


async def fetch_dataset(client, cfg):
    url = cfg.orchestra_url + "/v0/dataset"
    HEADERS = {
        "accept": "application/json",
        "Authorization": f"Bearer {cfg.api_key}",
        "Content-Type": "application/json",
    }
    params = {"name": cfg.dataset}
    response = await client.get(url, headers=HEADERS, params=params)
    prompts = response.json()
    return prompts


## IN PROGRESS
# TODO: make the response data have the right format (num tokns etc)
# TODO: change id_ to id


async def upload_responses(client, cfg, responses_path):
    to_upload = []
    with open(responses_path) as f:
        for line in f:
            to_upload.append(json.loads(line))

    # TODO: async over this
    for resp in to_upload:
        await upload_single_response(client, cfg, resp)


ADMIN_KEY = ""
async def upload_single_response(client, cfg, data):
    url = cfg.orchestra_url + "/v0/evaluations/upload_responses"
    HEADERS = {
        "accept": "application/json",
        "Authorization": f"Bearer {ADMIN_KEY}",
        "Content-Type": "application/json",
    }
    encoding = tiktoken.get_encoding("cl100k_base")
    num_tokens = encoding.encode(data["model_response"])

    params = {
        "prompt_id": data["id"],
        "endpoint_str": data["endpoint"],
        "response": data["model_response"],
        "num_tokens": num_tokens,
    }
    response = await client.post(url, headers=HEADERS, params=params)
    assert response.status_code == 200, response.json()


async def upload_judgements(client, cfg, judgements_path):
    to_upload = []
    with open(judgements_path) as f:
        for line in f:
            to_upload.append(json.loads(line))

    # TODO: async over this
    for jgmt in to_upload:
        await upload_single_judgement(client, cfg, jgmt)


async def upload_single_judgement(client, cfg, data):
    url = cfg.orchestra_url + "/v0/evaluations/upload_judgements"
    HEADERS = {
        "accept": "application/json",
        "Authorization": f"Bearer {ADMIN_KEY}",
        "Content-Type": "application/json",
    }

    params = {
        "prompt_id": data["id"],
        "endpoint_str": data["endpoint"],
        "evaluator_id": cfg.evaluator_id,
        "judge_endpoint_id": data["judge_endpoint"],
        "judgement": data["judge_response"],
        "score": data["score"],
    }
    response = await client.post(url, headers=HEADERS, params=params)
    assert response.status_code == 200, response.json()


##


async def evaluate_dataset(msg, data_dir, shared_volume="", client=None):
    """msg is a json object with two fields: config and prompts
    prompts is a list of json objects of the form {"prompt", "reference_answer"}.
    """
    cfg = json.loads(msg)
    user_email = cfg.pop("user_email", None)
    cfg = BenchmarkConfig(**cfg)
    if client is None:
        limits = Limits(
            max_keepalive_connections=None, max_connections=None, keepalive_expiry=30
        )
        client = AsyncClient(base_url=cfg.orchestra_url, limits=limits, timeout=60)

    # create root folder
    run_save_path = os.path.join(shared_volume, data_dir, cfg.user_id, cfg.dataset)

    os.makedirs(run_save_path, exist_ok=True)

    model_responses_path = os.path.join(run_save_path, "model_responses")
    model_judgements_path = os.path.join(run_save_path, "model_judgements")
    prompts_path = os.path.join(run_save_path, "prompts.jsonl")

    # load prompts
    prompts = await fetch_dataset(client, cfg)
    os.makedirs(model_responses_path, exist_ok=True)
    os.makedirs(model_judgements_path, exist_ok=True)
    if not os.path.isfile(prompts_path):
        # make prompt file
        with open(prompts_path, "w") as f:
            for entry in prompts:
                f.write(json.dumps(entry) + "\n")

    # load eval_config
    eval_config = await fetch_evaluator_config(client, cfg)

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

    # TODO :: CHANGE THE IN PROGRESS BLOB STUFF to use the db

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
            endpoint, judge_model_tag, cfg.evaluator
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
                "response_blob_name": "",
                "progress_blob_name": "",
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

    model_responses_formatted_path = os.path.join(
        model_responses_path,
        _format_model_tag(cfg.endpoint) + ".jsonl",
    )

    ## todo: uploads
    response = await upload_responses(client, cfg, model_responses_formatted_path)

    ## upload judgements

    for judge_tag in judge_models:
        fmtd_judge_tag = _format_model_tag(judge_tag)
        blob_name = create_judgement_blob_filename(cfg.endpoint, cfg.evaluator, judge_tag)

        model_judgements_formatted_path = os.path.join(
            model_judgements_path,
            _format_judgements_file(cfg.endpoint, judge_tag, cfg.evaluator) + ".jsonl",
        )
        response = await upload_judgements(client, cfg, model_judgements_formatted_path)


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
