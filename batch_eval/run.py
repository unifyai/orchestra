# run.py
import sys
import os
import json
import asyncio

import smtplib
from email.message import EmailMessage

import requests
from google.cloud import aiplatform

from fetch_queries import generate_queries
from token_counts import count_tokens
from fetch_judgements import generate_judgements
from extract_score import ratings_from_sample


async def main():
    root_dir = sys.argv[1]
    prompt_file = sys.argv[2]
    api_key = sys.argv[3]
    name = sys.argv[4]
    user_id = sys.argv[5]
    user_email = sys.argv[6]

    model_list = [
        "mixtral-8x7b-instruct-v0.1@together-ai",
        "mixtral-8x22b-instruct-v0.1@together-ai",
        "gpt-3.5-turbo@openai",
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
    judge_model = "gpt-4-turbo@openai"

    # Get router scores
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
            out["gpt-4"] = out.pop("gpt-4-0125-preview")

            router_scores[ix] = out

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

    # Create tasks for each model
    tasks = [
        process_queries(model_tag, prompt_file, root_dir, api_key)
        for model_tag in model_list
    ]

    # Run tasks in parallel
    await asyncio.gather(*tasks)

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

    # Create tasks for each model
    tasks = [
        process_judgements(model_tag, prompt_file, root_dir, api_key)
        for model_tag in model_list
    ]

    # Run tasks in parallel
    await asyncio.gather(*tasks)

    ## get the model_judgements
    for model_tag in model_list:
        model_name = model_tag.split("@")[0]

    ## Do token counts

    id_model_to_tokens = count_tokens(root_dir=root_dir)

    ## creates the final table
    id_to_model_to_scores = {}

    for model_tag in model_list:
        model_name = model_tag.split("@")[0]
        judge_response_file = f"{root_dir}/model_judgements/{model_name}.jsonl"
        if os.path.exists(judge_response_file):
            with open(judge_response_file) as f:
                for line in f:
                    data = json.loads(line)
                    judge_response = data["judge_response"]
                    score = ratings_from_sample(judge_response)
                    if data["id_"] not in id_to_model_to_scores:
                        id_to_model_to_scores[data["id_"]] = {}
                    id_to_model_to_scores[data["id_"]][model_name] = score

    # TODO: If anything goes wrong, set the dataset evaluation status to failed

    url = f'{os.getenv("ORCHESTRA_BASE_URL")}/v0/admin/create_dataset_evaluation'
    headers = {"Authorization": f'Bearer {os.getenv("ORCHESTRA_ADMIN_KEY")}'}
    for prompt_id, model_scores in id_to_model_to_scores.items():
        for model_name, score in model_scores.items():
            payload = {
                "mdl_name": model_name,
                "dataset_name": name,
                "prompt": str(prompt_id),
                "gt_score": score,
                "score": router_scores[prompt_id][model_name],
                "input_tokens": id_model_to_tokens[prompt_id, model_name]["num_toks_in"],
                "output_tokens": id_model_to_tokens[prompt_id, model_name]["num_toks_out"],
            }
            response = requests.put(url, json=payload, headers=headers)

    print("Populating cache...")

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

    print("Marking task as complete")

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

    print("Task completed!")

    # Initialise email server
    email_server = smtplib.SMTP("smtp.gmail.com", 587)
    email_server.starttls()
    email_addr = os.getenv("EMAIL_ADDR", "auth@unify.ai")
    email_pass = os.getenv("EMAIL_PASS", "")
    email_server.login(email_addr, email_pass)
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
        <p>Your dataset evaluation is ready, you can check out the results in <a href="https://console.unify.ai">your console</a>.</p>
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

    msg = EmailMessage()
    msg["From"] = f"Unify <auth@unify.ai>"
    msg["To"] = user_email
    msg["Bcc"] = "guillermo@unify.ai"
    msg["Subject"] = "Your dataset evaluation is ready!"
    msg.set_content(body, subtype="html")

    email_server.send_message(msg)
    email_server.quit()

    print("Mail sent!")


if __name__ == "__main__":
    # Run the main coroutine
    asyncio.run(main())
