"""
Includes endpoints related to dataset evaluations.
"""

import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Query, Request
from google.cloud import storage
from providers.completion import PROVIDER_CLASSES

from orchestra.web.api.utils.gcp import (
    blob_exists,
    delete_dir,
    dir_exists,
    list_dir,
    send_pubsub_msg,
)
from orchestra.web.api.utils.http_responses import (
    dataset_does_not_exist,
    evaluation_does_not_exist,
    invalid_training_endpoints,
)

router = APIRouter()

# utils


# TODO: Move to utils (duplicated in dataset)
def _list_datasets(user_id: str):
    bucket_name = "uploaded_datasets"
    blobs = list_dir(bucket_name, user_id)
    dirs = set([b.id.split("/")[2] for b in blobs])
    # Clean legacy datasets
    dirs = {d for d in dirs if not d.endswith(".jsonl")}
    return list(dirs)


def _list_evaluations(user_id: str, dataset: str):
    bucket_name = "uploaded_datasets"
    blobs = list_dir(bucket_name, f"{user_id}/{dataset}")
    endpoints = []
    for b in blobs:
        # keep only the endpoints
        levels = b.id.split("/")
        if "judgements.jsonl" in b.id and len(levels) > 4:
            endpoints.append(levels[4])
    return endpoints


def _get_scores(user_id: str, dataset: str):
    bucket_name = "uploaded_datasets"
    storage_client = storage.Client()
    bucket = storage_client.bucket(bucket_name)
    blob = bucket.blob(f"{user_id}/{dataset}/0/scores.json")
    try:
        content = blob.download_as_bytes().decode("utf-8")
        return json.loads(content)
    except:
        raise evaluation_does_not_exist(dataset)


def _get_input_tokens(user_id: str, dataset: str):
    bucket_name = "uploaded_datasets"
    storage_client = storage.Client()
    bucket = storage_client.bucket(bucket_name)
    blob = bucket.blob(f"{user_id}/{dataset}/0/num_tokens.json")
    try:
        content = blob.download_as_bytes().decode("utf-8")
        return json.loads(content)["num_tokens"]
    except:
        return 1


def _get_response_tokens(user_id: str, dataset: str, endpoint: str):
    bucket_name = "uploaded_datasets"
    storage_client = storage.Client()
    bucket = storage_client.bucket(bucket_name)
    blob = bucket.blob(f"{user_id}/{dataset}/0/{endpoint}/num_tokens_in_responses.json")
    try:
        content = blob.download_as_bytes().decode("utf-8")
        return json.loads(content)["num_tokens"]
    except:
        return 1


# TODO: Move to utils (duplicated in routing)
def dataset_exists(user_id, name):
    # TODO: This needs to take public datasets into account as
    # well.
    bucket_name = "uploaded_datasets"
    blob_name = f"{user_id}/{name}/0/dataset.jsonl"
    if blob_exists(bucket_name, blob_name):
        return True
    return False


# TODO: Move to utils (duplicated in routing)
def is_standard_endpoint(model: str, provider: str):
    if provider in PROVIDER_CLASSES:
        lm = PROVIDER_CLASSES[provider](model)
        if model in lm.supported_models:
            return True
    return False


# TODO: Move to utils (duplicated in routing)
def find_invalid_endpoints(endpoints):
    invalid_endpoints = []
    for e in endpoints:
        model, provider = e.split("@")
        if "router" in model:
            invalid_endpoints.append(e)
            continue
        if provider == "custom":
            # TODO: Support this properly, probably all providers (including custom one)
            # can have a method to check if the model exists as an endpoint
            # We also need an endpoint to list all public + custom endpoints
            invalid_endpoints.append(e)  # temp
            continue
        if not is_standard_endpoint(model, provider):
            invalid_endpoints.append(e)  # temp
            continue
    return invalid_endpoints


def _delete_evaluation(user_id: str, dataset: str, endpoint: str):
    bucket_name = "uploaded_datasets"
    # TODO: 0 will need to be accounted when introducing dynamic datasets
    if dataset == "":
        raise dataset_does_not_exist(dataset)
    dir_name = f"{user_id}/{dataset}/0/{endpoint}"
    if not dir_exists(bucket_name, dir_name):
        raise evaluation_does_not_exist(dataset)
    else:
        delete_dir(bucket_name, dir_name)


def send_to_dataset_evaluation_server(action, **data):
    topic = "projects/saas-368716/topics/dataset_evaluation"
    url = "https://api.unify.ai"  # TODO: Deal with staging/test
    send_pubsub_msg(topic, {"action": action, **data, "orchestra_url": url})


# endpoints

# TODO: Update dataset evaluation


@router.post(
    "/evaluation",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {
                        "info": "Dataset evaluation started! You will receive an email soon!",
                    },
                },
            },
        },
        400: {
            "description": "Invalid Endpoints",
            "content": {
                "application/json": {
                    "example": {
                        "detail": (
                            "Invalid input. Couldn't find"
                            " endpoints [model_1@endpoint_1, model_2@endpoint_2]."
                        ),
                    },
                },
            },
        },
        404: {
            "description": "Dataset Not Found",
            "content": {
                "application/json": {
                    "example": {"detail": "This dataset does not exist!"},
                },
            },
        },
    },
)
def evaluate_dataset(
    request_fastapi: Request,
    dataset: str = Body(..., description="Name of the uploaded dataset to evaluate."),
    endpoint: str = Body(
        ...,
        description=(
            "Endpoint to evaluate."
            " Endpoints must be specified using the `model@provider` format."
        ),
    ),
    judge_models: list[str] = Body(
        default=["gpt-4o@openai"],
        description="List of the LLMs to use as a judge",
    ),
    system_prompt: str = Body(
        default="",
        description="Optionally change the system prompt",
    ),
    class_cfg: list[dict[str, Any]] = Body(
        default=[],
        description="A description of the classes for judging.",
    ),
) -> Dict[str, str]:
    """
    Evaluates the quality of the responses from a given LLM endpoint in a custom dataset.
    """
    user_id = request_fastapi.state.user_id
    user_email = request_fastapi.state.user_email
    api_key = request_fastapi.headers["authorization"].removeprefix("Bearer ")
    # Check if the dataset exists
    if not dataset_exists(user_id, dataset):
        raise dataset_does_not_exist(dataset)
    # Check that the endpoints are valid
    invalid_endpoints = find_invalid_endpoints([endpoint])
    if invalid_endpoints:
        raise invalid_training_endpoints(invalid_endpoints)
    # Send train job to the dataset_evaluation server
    send_to_dataset_evaluation_server(
        action="evaluate",
        user_id=user_id,
        user_email=user_email,
        api_key=api_key,
        dataset=dataset,
        endpoint=endpoint,
        judge_models=judge_models,
        system_prompt=system_prompt,
        class_cfg=class_cfg,
    )
    return {"info": "Dataset evaluation started! You will receive an email soon!"}


@router.delete(
    "/evaluation",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {"info": "Dataset evaluation deleted!"},
                },
            },
        },
    },
)
def delete_dataset_evaluation(
    request_fastapi: Request,
    dataset: str = Query(
        ...,
        description="Name of the dataset to delete an evaluation from.",
    ),
    endpoint: str = Query(
        ...,
        description="Endpoint whose evaluation will be deleted.",
    ),
) -> Dict[str, str]:
    """
    Deletes a specific dataset evaluation quality score
    and the corresponding artifacts.
    """
    user_id = request_fastapi.state.user_id
    # Delete the dataset_evaluation files
    _delete_evaluation(user_id, dataset, endpoint)
    # TODO: Move this to the microservice
    # send_to_dataset_evaluation_server(
    #     action="delete",
    #     user_id=user_id,
    #     dataset=dataset,
    #     endpoint=endpoint,
    # )
    return {"info": "Dataset evaluation deleted!"}


@router.get(
    "/evaluation/list",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": [
                        {
                            "dataset_1": [
                                "model_1@provider_1",
                                "model_2@provider_2",
                                "...",
                            ],
                        },
                        {"dataset_2": ["..."]},
                        {"...": ["..."]},
                    ],
                },
            },
        },
        404: {
            "description": "Dataset Not Found",
            "content": {
                "application/json": {
                    "example": {"detail": "This dataset does not exist!"},
                },
            },
        },
    },
)
def get_dataset_evaluations(
    request_fastapi: Request,
    dataset: Optional[str] = Query(
        None,
        description=(
            "Name of the dataset to fetch evaluation from."
            " If not specified, all evaluations will be returned."
        ),
    ),
) -> Dict[str, List[str]]:
    """
    Fetches a list of the endpoints that have been evaluated on a given dataset.
    """
    user_id = request_fastapi.state.user_id
    if dataset is not None:
        # Check if the dataset exists
        if not dataset_exists(user_id, dataset):
            raise dataset_does_not_exist(dataset)
    evaluations = {}
    datasets = [dataset] if dataset is not None else _list_datasets(user_id)
    for d in datasets:
        evaluations[d] = _list_evaluations(user_id, d)
    return evaluations


@router.get(
    "/evaluation/results",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": [
                        {
                            "judge": {
                                "model_1@provider_1": "score_1",
                                "model_2@provider_2": "score_2",
                            },
                            "input_tokens": "num_tokens_in_dataset",
                            "output_tokens": {
                                "model_1@provider_1": "num_tokens_in_endpoint_responses",
                            },
                        },
                    ],
                },
            },
        },
        404: {
            "description": "Dataset Not Found",
            "content": {
                "application/json": {
                    "example": {"detail": "This dataset does not exist!"},
                },
            },
        },
    },
)
def get_dataset_evaluation_results(
    request_fastapi: Request,
    dataset: Optional[str] = Query(
        None,
        description=("Name of the dataset to fetch evaluation from."),
    ),
) -> Dict:
    """
    Fetches the results of a given dataset evaluation.
    """
    user_id = request_fastapi.state.user_id
    if not dataset_exists(user_id, dataset):
        raise dataset_does_not_exist(dataset)
    scores = _get_scores(user_id, dataset)
    if not isinstance(scores, dict):
        return scores
    output_tokens = {}
    for judge in scores.keys():
        for endpoint in scores[judge].keys():
            output_tokens[endpoint] = _get_response_tokens(
                user_id,
                dataset,
                endpoint,
            )
    scores["input_tokens"] = _get_input_tokens(user_id, dataset)
    scores["output_tokens"] = output_tokens

    return scores
