# TODO:
# Get rid of the sys.path stuff
# Use AsyncClient (so can be tested on staging etc)

import asyncio
import json
import pytest
import requests
import time
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from dataset_evaluation import evaluate_dataset

orchestra_base_url = "https://api.unify.ai/v0"

api_key = str(os.getenv("AUTH_ACCOUNT_API_KEY"))
auth_user_id = str(os.getenv("AUTH_ACCOUNT_USER_ID"))

HEADERS = {
    "accept": "application/json",
    "Authorization": f"Bearer {api_key}",
}


def _create_default_cfg():
    msg = {
        "endpoint": "llama-3-8b-chat@aws-bedrock",
        "judge_models": ["claude-3-haiku@aws-bedrock"],
        "user_id": auth_user_id,
        "api_key": api_key,
        "orchestra_url": "https://api.unify.ai",
        "system_prompt": None,
        "class_cfg": None,
    }
    return msg


test_sample_data_path = "dataset_evaluation/tests/sample_data/test_data.jsonl"


def _create_dataset_evaluation(dataset_name, data_path):
    url = f"{orchestra_base_url}/dataset"
    data = {"name": dataset_name}
    with open(data_path, "rb") as f:
        file_content = f.read()
    files = {"file": ("test.jsonl", file_content, "application/x-jsonlines")}
    response = requests.post(url, data=data, files=files, headers=HEADERS)
    assert response.status_code == 200


def _load_dataset_score(dataset_name, endpoint, judge_model):
    url = f"{orchestra_base_url}/evaluation/results?dataset={dataset_name}"
    response = requests.get(url, headers=HEADERS)
    scores = json.loads(response.text)
    ret = scores[judge_model][endpoint]
    assert isinstance(ret, float)


def _delete_dataset_evaluation(dataset_name):
    url = f"{orchestra_base_url}/dataset"
    response = requests.delete(url, params={"name": dataset_name}, headers=HEADERS)
    assert response.status_code == 200


def _cleanup(dataset_name):
    # TODO: delete the files created locally
    pass


def _run_test_dataset_evaluation(msg):
    asyncio.run(evaluate_dataset(msg=json.dumps(msg), data_dir="save_files/"))


def _generic_test_dataset_evaluation(**kwargs):
    dataset_name = f"pytest_test_dataset_{int(time.time()*1000 % 1000)}"
    msg = _create_default_cfg()
    msg.update(kwargs)
    msg["dataset"] = dataset_name

    _create_dataset_evaluation(dataset_name, test_sample_data_path)
    _run_test_dataset_evaluation(msg)

    for judge_model in msg["judge_models"]:
        _load_dataset_score(dataset_name, msg["endpoint"], judge_model)

    _delete_dataset_evaluation(dataset_name)
    _cleanup(dataset_name)


def test_basic_dataset_evaluation():
    _generic_test_dataset_evaluation()


def test_two_judges_dataset_evaluation():
    _generic_test_dataset_evaluation(
        judge_models=["llama-3-8b-chat@aws-bedrock", "claude-3-haiku@aws-bedrock"]
    )


def test_system_prompt():
    # TODO
    pass


def test_class_config():
    # TODO
    pass


# if __name__ == "__main__":
#     _generic_test_dataset_evaluation()
