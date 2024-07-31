import requests
import os
import time
import json

orchestra_base_url = "https://api.unify.ai/v0"
api_key = str(os.getenv("AUTH_ACCOUNT_API_KEY"))

HEADERS = {
    "accept": "application/json",
    "Authorization": f"Bearer {api_key}",
}


def _upload_dataset(dataset_name, data_path):
    url = f"{orchestra_base_url}/dataset"
    data = {"name": dataset_name}
    with open(data_path, "rb") as f:
        file_content = f.read()
    files = {"file": ("test.jsonl", file_content, "application/x-jsonlines")}
    response = requests.post(url, data=data, files=files, headers=HEADERS)
    assert response.status_code == 200


def _delete_dataset_evaluation(dataset_name):
    url = f"{orchestra_base_url}/dataset"
    response = requests.delete(url, params={"name": dataset_name}, headers=HEADERS)
    assert response.status_code == 200


sample_path = "./orchestra/tests/sample_datasets/with_ref.jsonl"


# tests /evaluation
def test_evaluation():
    assert False

    # upload dataset
    dataset_name = f"test_dataset_EVALUATION_{int(time.time()*1000 % 1000)}"
    _upload_dataset(dataset_name, sample_path)
    time.sleep(5)

    # evaluate dataset
    url = f"{orchestra_base_url}/evaluation"
    endpoint = "llama-3-8b-chat@aws-bedrock"
    judge_models = ["claude-3-haiku@aws-bedrock"]
    params = {
        "dataset": dataset_name,
        "endpoint": endpoint,
        "judge_models": judge_models,
    }
    response = requests.post(url, json=params, headers=HEADERS)
    time.sleep(30)

    # check evaluation in list
    url = f"{orchestra_base_url}/evaluation/list"
    response = requests.get(url, headers=HEADERS)
    assert dataset_name in json.loads(response.text)

    # check evaluation in results
    url = f"{orchestra_base_url}/evaluation/results?dataset={dataset_name}"
    response = requests.get(url, headers=HEADERS)
    assert response.status_code == 200

    # cleanup
    # TODO: move this to a fixture
    _delete_dataset_evaluation(dataset_name)


# tests DELETE /evaluation
def test_evaluation_delete():
    assert False
    # upload dataset
    dataset_name = f"test_dataset_DELETE_{int(time.time()*1000 % 1000)}"
    _upload_dataset(dataset_name, sample_path)
    time.sleep(5)
    # trigger evaluation
    url = f"{orchestra_base_url}/evaluation"
    endpoint = "llama-3-8b-chat@aws-bedrock"
    judge_models = ["claude-3-haiku@aws-bedrock"]
    params = {
        "dataset": dataset_name,
        "endpoint": endpoint,
        "judge_models": judge_models,
    }
    response = requests.post(url, json=params, headers=HEADERS)
    time.sleep(30)

    # check in list and results

    url = f"{orchestra_base_url}/evaluation/list"
    response = requests.get(url, headers=HEADERS)
    assert dataset_name in json.loads(response.text)

    # check in results
    url = f"{orchestra_base_url}/evaluation/results?dataset={dataset_name}"
    response = requests.get(url, headers=HEADERS)
    assert response.status_code == 200, f"{response.text}"

    # delete evaluation
    _delete_dataset_evaluation(dataset_name)

    time.sleep(5)

    # check not in list
    url = f"{orchestra_base_url}/evaluation/list"
    response = requests.get(url, headers=HEADERS)
    assert dataset_name not in json.loads(response.text)

    # check not in results
    url = f"{orchestra_base_url}/evaluation/results?dataset={dataset_name}"
    response = requests.get(url, headers=HEADERS)
    assert response.status_code != 200


# /evaluation/list and /evaluation/results
# are tested implicitly in evaluate & delete
