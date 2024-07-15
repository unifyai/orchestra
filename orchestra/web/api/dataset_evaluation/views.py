"""
Includes endpoints related to dataset evaluations.
"""

from typing import Dict, List, Optional

from fastapi import APIRouter, Query, Request
from providers.completion import PROVIDER_CLASSES

from orchestra.web.api.utils.gcp import blob_exists, list_dir, send_pubsub_msg
from orchestra.web.api.utils.http_responses import (
    dataset_does_not_exist,
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


def send_to_dataset_evaluation_server(action, **data):
    topic = "projects/saas-368716/topics/dataset_evaluation"
    url = "https://api.unify.ai"  # TODO: Deal with staging/test
    send_pubsub_msg(topic, {"action": action, **data, "orchestra_url": url})


# endpoints

# TODO: Update dataset evaluation


@router.post("/evaluation")
def evaluate_dataset(
    request_fastapi: Request,
    dataset: str = Query(..., description="Name of the uploaded dataset to evaluate."),
    endpoint: str = Query(
        ...,
        description=(
            "Endpoint to evaluate."
            " Endpoints must be specified using the `model@provider` format."
        ),
    ),
) -> Dict[str, str]:
    """
    Evaluates the output quality of a given LLM endpoint in a custom dataset.
    """
    user_id = request_fastapi.state.user_id
    api_key = request_fastapi.headers["authorization"].removeprefix("Bearer ")
    # Check if the dataset exists
    if not dataset_exists(user_id, dataset):
        raise dataset_does_not_exist
    # Check that the endpoints are valid
    invalid_endpoints = find_invalid_endpoints([endpoint])
    if invalid_endpoints:
        raise invalid_training_endpoints(invalid_endpoints)
    # Send train job to the dataset_evaluation server
    send_to_dataset_evaluation_server(
        action="evaluate",
        user_id=user_id,
        api_key=api_key,
        dataset=dataset,
        endpoint=endpoint,
    )
    return {"info": "Dataset evaluation started! You will receive an email soon!"}


@router.delete("/evaluation")
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
    send_to_dataset_evaluation_server(
        action="delete",
        user_id=user_id,
        dataset=dataset,
        endpoint=endpoint,
    )
    return {"info": "Dataset evaluation deleted!"}


@router.get("/evaluation/list")
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
            raise dataset_does_not_exist
    evaluations = {}
    datasets = [dataset] if dataset is not None else _list_datasets(user_id)
    for d in datasets:
        evaluations[d] = _list_evaluations(user_id, d)
    return evaluations
