"""
Includes endpoints for training and deployment of a router.
"""

from typing import Dict, List

from fastapi import APIRouter, Request
from providers.completion import PROVIDER_CLASSES

from orchestra.web.api.utils.gcp import blob_exists, send_pubsub_msg
from orchestra.web.api.utils.http_responses import (
    dataset_does_not_exist,
    invalid_training_endpoints,
    router_already_deployed,
    router_is_not_deployed,
    router_training_already_exists,
    router_training_does_not_exist,
)

router = APIRouter()

# utils


def dataset_exists(user_id, name):
    # TODO: This needs to take public datasets into account as
    # well.
    bucket_name = "uploaded_datasets"
    blob_name = f"{user_id}/{name}/0/dataset.jsonl"
    if blob_exists(bucket_name, blob_name):
        return True
    return False


def router_training_exists(user_id, name):
    # TODO: The router directory with files needs a
    # metadata.json with the datasets it has been trained on
    bucket_name = "custom_router_data"
    dir = f"custom_router/{user_id}/{name}/"
    files = ["config.yaml", "model_mapping.json", "model.pth"]
    for f in files:
        if not blob_exists(bucket_name, dir + f):
            return False
    return True


def router_is_deployed():
    raise NotImplementedError


def is_standard_endpoint(model: str, provider: str):
    if provider in PROVIDER_CLASSES:
        lm = PROVIDER_CLASSES["provider"](model)
        if model in lm.supported_models:
            return True
    return False


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


def send_to_train_server(action, **data):
    topic = "projects/saas-368716/topics/train_router"
    url = "https://api.unify.ai"  # TODO: Deal with staging/test
    send_pubsub_msg(topic, {"action": action, **data, "orchestra_url": url})


def send_to_deploy_server(action, **data):
    topic = "projects/saas-368716/topics/deploy_router"
    url = "https://api.unify.ai"  # TODO: Deal with staging/test
    send_pubsub_msg(topic, {"action": action, **data, "orchestra_url": url})


# endpoints

# TODO: Allow not sending an email for the website + composed flows
# TODO: List trained routers


@router.post("router/train")
def train_router(
    request_fastapi: Request,
    name: str,
    dataset: str,
    endpoints: List[str],
) -> Dict[str, str]:
    user_id = request_fastapi.state.user_id
    # Check if the router already exists
    if router_training_exists(user_id, name):
        raise router_training_already_exists
    # Check if the dataset exists
    if not dataset_exists(user_id, dataset):
        raise dataset_does_not_exist
    # Check that the endpoints are valid
    invalid_endpoints = find_invalid_endpoints(endpoints)
    if invalid_endpoints:
        raise invalid_training_endpoints(invalid_endpoints)
    # Send train job to the training server
    send_to_train_server(
        action="train",
        user_id=user_id,
        name=name,
        dataset=dataset,
        endpoints=endpoints,
    )
    return {"info": "Router training started! You will receive an email soon!"}


@router.delete("/router/train")
def delete_router_train(request_fastapi: Request, name: str) -> Dict[str, str]:
    """
    Deactivates and deletes a trained router.
    """
    user_id = request_fastapi.state.user_id
    # Check if the router files exist
    if not router_training_exists(user_id, name):
        raise router_training_does_not_exist
    # Delete the trained router files
    send_to_train_server(action="delete", user_id=user_id, name=name)
    #   delete_training_files(router)
    #   set_router_training_status("deleted")
    #   router training -> id, user_id, name, dataset, status
    return {"info": "Trained router deleted!"}


# TODO: List deployed routers


@router.post("/router/deploy")
def deploy_router(request_fastapi: Request, name: str) -> Dict[str, str]:
    """
    Deploys a router.
    """
    user_id = request_fastapi.state.user_id
    # Check if the files exist
    if not router_training_exists(user_id, name):
        raise router_training_does_not_exist
    # Check if the router is already deployed
    if router_is_deployed(router):
        raise router_already_deployed
    # Send the request with the job to the router deployment service
    send_to_deploy_server(action="deploy", user_id=user_id, name=name)
    return {"info": "Router deployment started! You will receive an email soon!"}


@router.delete("/router/deploy")
def delete_router(request_fastapi: Request, name: str) -> Dict[str, str]:
    """
    Deactivates and deletes a deployed router.
    """
    user_id = request_fastapi.state.user_id
    # Check if the router exists
    if not router_is_deployed(router):
        raise router_is_not_deployed
    # Send the request with the job to the router deployment service
    send_to_deploy_server(action="delete", user_id=user_id, name=name)
    #   un-deploy router
    #   modify entry in the db
    return {"info": "Router deletion started! You will receive an email soon!"}
