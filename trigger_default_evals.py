import os
from typing import List

import requests


def get_evaluated_endpoints(api_key: str):
    url = "https://api.unify.ai/v0/evaluation?dataset=Open+Hermes&evaluator=default_evaluator"
    headers = {"Authorization": f"Bearer {api_key}"}
    response = requests.request("GET", url, headers=headers)
    return list(response.json()["default_evaluator"].keys())


def get_all_endpoints(api_key: str):
    url = "https://api.unify.ai/v0/endpoints"
    headers = {"Authorization": f"Bearer {api_key}"}
    response = requests.request("GET", url, headers=headers)
    return response.json()


def trigger_evals_for_endpoints(api_key: str, endpoints: List[str]):
    for endpoint in endpoints:
        url = f"https://api.unify.ai/v0/evaluation?dataset=Open+Hermes&endpoint={endpoint}"
        headers = {"Authorization": f"Bearer {api_key}"}
        data = {}
        response = requests.request("POST", url, data=data, headers=headers)
        print(endpoint, response.text)


if __name__ == "__main__":
    api_key = os.environ.get("API_KEY")
    eval_endpoints = get_evaluated_endpoints(api_key)
    all_endpoints = get_all_endpoints(api_key)
    non_triggered_endpoints = list(set(all_endpoints).difference(eval_endpoints))
    trigger_evals_for_endpoints(non_triggered_endpoints)
