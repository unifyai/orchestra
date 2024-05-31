# run.py
import asyncio
import json
import os
import sys
import time

import smtplib
from email.message import EmailMessage

import requests
from fetch_queries import generate_queries
from fetch_judgements import generate_judgements
from extract_score import ratings_from_sample
from token_counts import count_tokens
from google.cloud import aiplatform

def send_email(user_email):
    email_server = smtplib.SMTP("smtp.gmail.com", 587)
    email_server.starttls()
    email_addr = os.getenv("EMAIL_ADDR", "auth@unify.ai")
    email_pass = os.getenv("EMAIL_PASS", "")
    email_server.login(email_addr, email_pass)

    with open("data/email_body.txt") as f:
        body = f.read()

    msg = EmailMessage()
    msg["From"] = f"Unify <auth@unify.ai>"
    msg["To"] = user_email
    msg["Bcc"] = "guillermo@unify.ai"
    msg["Subject"] = "Your dataset evaluation is ready!"
    msg.set_content(body, subtype="html")

    email_server.send_message(msg)
    email_server.quit()


async def main():
    root_dir = sys.argv[1]
    prompt_file = sys.argv[2]
    api_key = sys.argv[3]
    name = sys.argv[4]
    user_id = sys.argv[5]
    user_email = sys.argv[6]

    def log_msg(msg):
        time_string = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())
        print(f'[[{time_string}]] [[{name}]] {msg}')

    DEBUG = os.getenv("DEBUG", "false").lower() == "true"

    log_msg(f"Beginning benchmark")

    model_list = [
        "mixtral-8x7b-instruct-v0.1@together-ai",
        "mixtral-8x22b-instruct-v0.1@together-ai",
        "gpt-3.5-turbo@openai",
        "gpt-4@openai",
        "gpt-4-turbo@openai",
        "gpt-4o@openai",
        "claude-3-haiku@anthropic",
        "claude-3-sonnet@anthropic",
        "claude-3-opus@anthropic",
        "deepseek-coder-33b-instruct@together-ai",
        "llama-3-8b-chat@together-ai",
        "llama-3-70b-chat@together-ai",
        "mistral-small@mistral-ai",
        "mistral-large@mistral-ai",
        "gemma-7b-it@together-ai",
    ]
    judge_model = "gpt-4o@openai"

    if not DEBUG:
        # Get router scores
        log_msg('Getting router scores...')
        aiplatform.init(
            project=os.getenv("ORCHESTRA_VERTEXAI_PROJECT"),
            location=os.getenv("ORCHESTRA_VERTEXAI_LOCATION"),
        )
        endpoint = aiplatform.Endpoint(os.getenv("ORCHESTRA_VERTEXAI_ROUTER_ENDPOINT_ID"))
        router_scores = {}
        with open(prompt_file, "r") as pf:
            for ix, line in enumerate(pf):
                data = json.loads(line)
                prediction = endpoint.predict(instances=[{"prompt": data["prompt"]}])
                out = prediction.predictions[0]["scores"]
                out["gpt-4-turbo"] = out.pop("gpt-4-0125-preview")
                router_scores[ix] = out

        log_msg('Obtained all router scores')

    if not os.path.isdir(root_dir):
        os.mkdir(root_dir)
        os.mkdir(f"{root_dir}/model_responses")
        os.mkdir(f"{root_dir}/model_judgements")

    async def process_queries(model_tag, prompt_file, root_dir, api_key):
        model_name = model_tag.split("@")[0]
        await generate_queries(
            prompt_file=prompt_file,
            response_file=f"{root_dir}/model_responses/{model_name}.jsonl",
            model_tag=model_tag,
            batch_size=5,
            api_key=api_key,
        )

    tasks = [
        process_queries(model_tag, prompt_file, root_dir, api_key)
        for model_tag in model_list
    ]

    # Run tasks in parallel
    log_msg('Beginning getting queries')
    await asyncio.gather(*tasks)
    log_msg('Ended getting queries')

    async def process_judgements(model_tag, prompt_file, root_dir, api_key):
        model_name = model_tag.split("@")[0]
        await generate_judgements(
            prompt_file=prompt_file,
            asst_response_file=f"{root_dir}/model_responses/{model_name}.jsonl",
            judge_response_file=f"{root_dir}/model_judgements/{model_name}.jsonl",
            asst_model_tag=model_tag,
            judge_model_tag=judge_model,
            batch_size=2,
            api_key=api_key,
        )

    tasks = [
        process_judgements(model_tag, prompt_file, root_dir, api_key)
        for model_tag in model_list
    ]

    # Run tasks in parallel
    log_msg("Beginning getting judgements")
    await asyncio.gather(*tasks)
    log_msg("Ended getting judgements")

    ## Do token counts
    log_msg("Beginning counting tokens")
    id_model_to_tokens = count_tokens(root_dir=root_dir)
    log_msg("Ended counting tokens")

    ## Collating scores
    log_msg("Collating all judgements")
    id_to_model_to_scores = {}
    for model_tag in model_list:
        model_name = model_tag.split("@")[0]
        judge_response_file = f"{root_dir}/model_judgements/{model_name}.jsonl"
        if not os.path.exists(judge_response_file):
            log_msg("Judge response file does not exist")
            continue

        with open(judge_response_file) as f:
            for line in f:
                data = json.loads(line)
                judge_response = data["judge_response"]
                score = ratings_from_sample(judge_response)
                if data["id_"] not in id_to_model_to_scores:
                    id_to_model_to_scores[data["id_"]] = {}
                id_to_model_to_scores[data["id_"]][model_name] = score


    # TODO: If anything goes wrong, set the dataset evaluation status to failed

    if not DEBUG:
        url = f'{os.getenv("ORCHESTRA_BASE_URL")}/v0/admin/create_dataset_evaluation'
        headers = {"Authorization": f'Bearer {os.getenv("ORCHESTRA_ADMIN_KEY")}'}
        log_msg('Beginning uploading dataset')
        for prompt_id, model_scores in id_to_model_to_scores.items():
            for model_name, score in model_scores.items():
                router_score = 0
                if model_name in router_scores[prompt_id]:
                    router_score = router_scores[prompt_id][model_name]
                payload = {
                    "mdl_name": model_name,
                    "dataset_name": name,
                    "prompt": str(prompt_id),
                    "gt_score": score,
                    "score": router_score,
                    "input_tokens": id_model_to_tokens[prompt_id, model_name][
                        "num_toks_in"
                    ],
                    "output_tokens": id_model_to_tokens[prompt_id, model_name][
                        "num_toks_out"
                    ],
                }
                response = requests.put(url, json=payload, headers=headers)

        log_msg("Populating cache...")

        # send a request to populate the cache first
        url = f'{os.getenv("ORCHESTRA_BASE_URL")}/v0/get_dataset_evaluation'
        headers = {"Authorization": f"Bearer {api_key}"}
        payload = {"dataset_name": name}
        retry = 5
        while retry > 0:
            response = requests.get(url, params=payload, headers=headers)
            if response.text != "{}":
                break
            retry = retry - 1
            print("Retrying cache population...")

        log_msg("Marking task as complete")
        # very naive check that checks we got any model responses
        status = "completed" if id_to_model_to_scores else "failed"
        # mark the dataset as completed if success
        url = f'{os.getenv("ORCHESTRA_BASE_URL")}/v0/admin/update_dataset_evaluation_task'
        headers = {"Authorization": f'Bearer {os.getenv("ORCHESTRA_ADMIN_KEY")}'}
        payload = {
            "user_id": user_id,
            "name": name,
            "status": status,
        }
        response = requests.put(url, params=payload, headers=headers)

        log_msg("attempting to send email")
        send_email(user_email)
        log_msg("email sent")

    log_msg("Benchmark complete")


if __name__ == "__main__":
    # Run the main coroutine
    asyncio.run(main())
