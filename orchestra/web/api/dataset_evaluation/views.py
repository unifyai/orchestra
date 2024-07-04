"""
Includes endpoints related to dataset evaluations.
"""

from typing import Dict

from fastapi import APIRouter, Request
from providers.completion import PROVIDER_CLASSES

from orchestra.web.api.utils.gcp import blob_exists, send_pubsub_msg
from orchestra.web.api.utils.http_responses import (
    dataset_does_not_exist,
    invalid_training_endpoints,
)

router = APIRouter()

# utils


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

# TODO: List dataset evaluations
# TODO: Update dataset evaluation


@router.post("/evaluation")
def evaluate_dataset(
    request_fastapi: Request,
    dataset: str,
    endpoint: str,
) -> Dict[str, str]:
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
    dataset: str,
    endpoint: str,
) -> Dict[str, str]:
    """
    Deactivates and deletes a dataset evaluation.
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
