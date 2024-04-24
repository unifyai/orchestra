# run.py
import os
import json

from concurrent.futures import ThreadPoolExecutor

from fetch_queries import generate_queries
from fetch_judgements import generate_judgements
from extract_score import ratings_from_sample

prompt_file = "data/test.jsonl"
model_list = ["llama-3-8b-chat@together-ai", "llama-3-70b-chat@together-ai"]
root_dir = "tmp_data"
judge_model = "gpt-4-turbo"

if not os.path.isdir(root_dir):
    os.mkdir(root_dir)
    os.mkdir(f"{root_dir}/model_responses")
    os.mkdir(f"{root_dir}/model_judgements")

## get the model_responses
with ThreadPoolExecutor() as executor:
    tasks = []
    for model_tag in model_list:
        model_name = model_tag.split("@")[0]
        tasks.append(
            executor.submit(
                lambda: generate_queries(
                    prompt_file=prompt_file,
                    response_file=f"{root_dir}/model_responses/{model_name}.jsonl",
                    model_tag=model_tag,
                    batch_size=5,
                )
            )
        )
    for running_task in tasks:
        running_task.result()

## get the model_judgements
with ThreadPoolExecutor() as executor:
    tasks = []
    for model_tag in model_list:
        model_name = model_tag.split("@")[0]
        tasks.append(
            executor.submit(
                lambda: generate_judgements(
                    prompt_file=prompt_file,
                    asst_response_file=f"{root_dir}/model_responses/{model_name}.jsonl",
                    judge_response_file=f"{root_dir}/model_judgements/{model_name}.jsonl",
                    asst_model_tag=model_tag,
                    judge_model_tag=judge_model,
                    batch_size=10,
                )
            )
        )
    for running_task in tasks:
        running_task.result()


## creates the final table
id_to_model_to_scores = {}

for model_tag in model_list:
    model_name = model_tag.split("@")[0]
    judge_response_file = f"{root_dir}/model_judgements/{model_name}.jsonl"
    with open(judge_response_file) as f:
        for line in f:
            data = json.loads(line)
            judge_response = data["judge_response"]
            score = ratings_from_sample(judge_response)
            if data["id_"] not in id_to_model_to_scores:
                id_to_model_to_scores[data["id_"]] = {}
            id_to_model_to_scores[data["id_"]][model_name] = score

print(id_to_model_to_scores)
