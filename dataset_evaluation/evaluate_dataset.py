import asyncio
import json
import logging
import os
import smtplib
import sys
from dataclasses import dataclass
from email.message import EmailMessage

import tiktoken

from utils.fetch_queries import generate_response
from utils.fetch_judgements import generate_judgement


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
    admin_key: str


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

    # load eval_config
    eval_config = await fetch_evaluator_config(client, cfg)

    # TODO: change this, we only need the ids
    prompts = await fetch_dataset(client, cfg)

    BATCH_SIZE = 5  # TODO: change
    semaphore = asyncio.Semaphore(BATCH_SIZE)

    ## TODO: add exception handling of some sort
    # the generate_* functions return success/fail, and we could retry them here,
    tasks = [
        generate_response(p["id"], cfg.endpoint, cfg, client, semaphore)
        for p in prompts
    ]
    successful_responses = await asyncio.gather(*tasks)
    
    semaphore = asyncio.Semaphore(BATCH_SIZE)
    tasks = [
        generate_judgement(
            prompt_id=p["id"],
            endpoint_str=cfg.endpoint,
            cfg=cfg,
            eval_config=eval_config,
            client=client,
            semaphore=semaphore,
        )
        for p in prompts
    ]
    sucessful_judgements = await asyncio.gather(*tasks)

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
