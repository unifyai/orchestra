"""
Includes endpoints for training and deployment of a router.
"""

from typing import Dict, List, Union

from fastapi import APIRouter, Query, Request
from providers.completion import PROVIDER_CLASSES

from orchestra.web.api.utils.gcp import (
    blob_exists,
    list_dir,
    send_pubsub_msg,
    vertex_ai_endpoint_exists,
    vertex_ai_endpoint_list,
)
from orchestra.web.api.utils.http_responses import (
    dataset_does_not_exist,
    invalid_training_endpoints,
    router_already_deployed,
    router_is_not_deployed,
    router_training_already_exists,
    router_training_does_not_exist,
)
from orchestra.web.api.utils.on_prem import handle_on_prem

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


def router_is_deployed(user_id, name):
    # TODO: Implement checks to ensure no one can deploy a router if
    # the name is already in use.
    endpoint = f"{user_id}_{name}"
    if vertex_ai_endpoint_exists(endpoint):
        return True
    return False


def is_standard_endpoint(model: str, provider: str):
    if provider in PROVIDER_CLASSES:
        lm = PROVIDER_CLASSES[provider](model)
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


def _list_trained_routers(user_id: str):
    bucket_name = "custom_router_data"
    blobs = list_dir(bucket_name, f"custom_router/{user_id}")
    bucket = storage.Client().bucket(bucket_name)
    routers_metadata = {}
    for b in blobs:
        if "model.pth" in b.name:
            metadata_path = b.name.replace("model.pth", "metadata.json")
            metadata_blob = bucket.blob(metadata_path)
            try:
                metadata_contents = metadata_blob.download_as_bytes().decode("utf-8")
                metadata = json.loads(metadata_contents)
            except:
                metdata = {"dataset": "", "endpoints": [""]}
            router_name = b.id.split("/")[3]
            routers_metadata[router_name] = metadata
    return routers_metadata


def _list_deployed_routers(user_id: str):
    router_endpoints = vertex_ai_endpoint_list()
    clean_routers = []
    for r in router_endpoints:
        if user_id in r:
            clean_routers.append(r.removeprefix(f"{user_id}_"))
    return clean_routers


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


@router.post(
    "/router/train",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {
                        "info": "Router training started! You will receive an email soon!",
                    },
                },
            },
        },
        400: {
            "description": "Router Training Already Exist",
            "content": {
                "application/json": {
                    "example": {
                        "detail": (
                            "A router with this name has already been trained. Please, "
                            "choose a different one."
                        ),
                    },
                },
            },
        },
        404: {
            "description": "Dataset Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "This dataset does not exist.",
                    },
                },
            },
        },
    },
)
@handle_on_prem(endpoint="/router/train", method="post")
def train_router(
    request_fastapi: Request,
    name: str = Query(..., description="Name of the router."),
    dataset: str = Query(
        ...,
        description=(
            "Name of the dataset to train the router on."
            " To use a dataset, you need to first upload it to your account"
            " using the `/dataset` POST endpoints."
        ),
    ),
    endpoints: List[str] = Query(
        ...,
        description=(
            "List of endpoints to include in the router."
            " Endpoints must be specified using the `model@provider` format."
        ),
    ),
) -> Dict[str, str]:
    """
    Trains a router based on a dataset and a set of endpoints. To use a
    custom-trained router, you will need to deploy the resulting artifacts to
    a live endpoint. To do this, use the `/router/deploy` POST endpoint.
    """
    user_id = request_fastapi.state.user_id
    api_key = request_fastapi.headers["authorization"].removeprefix("Bearer ")
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
        api_key=api_key,
        name=name,
        dataset=dataset,
        endpoints=endpoints,
    )
    return {"info": "Router training started! You will receive an email soon!"}


@router.get(
    "/router/train/list",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {
                        "router_1": {
                            "dataset": "dataset_1",
                            "endpoints": ["model@provider", "..."],
                        },
                        "...": {"..."},
                    },
                },
            },
        },
    },
)
@handle_on_prem(endpoint="/router/train/list", method="get")
def get_trained_routers(
    request_fastapi: Request,
) -> Dict[str, Dict[str, Union[str, List[str]]]]:
    """
    Fetches a list of the trained routers and relevant metadata. These routers are training
    artifacts and therefore don't imply an active, deployed router. To fetch a list of
    deployed routers, you can use the /router/deploy/list GET endpoint.
    """
    user_id = request_fastapi.state.user_id
    routers_metadata = _list_trained_routers(user_id)
    return routers_metadata


@router.delete(
    "/router/train",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {"example": {"info": "Trained router deleted!"}},
            },
        },
        400: {
            "description": "Router Training Does Not Exist",
            "content": {
                "application/json": {
                    "example": {
                        "detail": (
                            "This router training doesn't exist. "
                            "Please, choose a different one or trigger the training first."
                        ),
                    },
                },
            },
        },
    },
)
@handle_on_prem(endpoint="/router/train", method="delete")
def delete_router_train(
    request_fastapi: Request,
    name: str = Query(..., description="Name of the router to delete."),
) -> Dict[str, str]:
    """
    Deletes the training files of a specific router.
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


@router.post(
    "/router/deploy",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {
                        "info": "Router deployment started! You will receive an email soon!",
                    },
                },
            },
        },
        400: {
            "description": "Router Training Does Not Exist",
            "content": {
                "application/json": {
                    "example": {
                        "detail": (
                            "This router training doesn't exist. "
                            "Please, choose a different one or trigger the training first."
                        ),
                    },
                },
            },
        },
    },
)
@handle_on_prem(endpoint="/router/deploy", method="post")
def deploy_router(
    request_fastapi: Request,
    name: str = Query(..., description="Name of the router to deploy."),
) -> Dict[str, str]:
    """
    Deploys a trained router to a live endpoint.

    To use this router, replace the model in the endpoint string with the
    router name. E.g. you can use `router-abc` by calling the
    `router-abc@q:1` endpoint.

    """
    user_id = request_fastapi.state.user_id
    # Check if the files exist
    if not router_training_exists(user_id, name):
        raise router_training_does_not_exist
    # Check if the router is already deployed
    if router_is_deployed(user_id, name):
        raise router_already_deployed
    # Send the request with the job to the router deployment service
    send_to_deploy_server(action="deploy", user_id=user_id, name=name)
    return {"info": "Router deployment started! You will receive an email soon!"}


@router.delete(
    "/router/deploy",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {
                        "info": "Router deletion started! You will receive an email soon!",
                    },
                },
            },
        },
        400: {
            "description": "Router Not Deployed",
            "content": {
                "application/json": {
                    "example": {"detail": "This router is not deployed!"},
                },
            },
        },
    },
)
@handle_on_prem(endpoint="/router/deploy", method="delete")
def delete_router(
    request_fastapi: Request,
    name: str = Query(..., description="Name of the router to un-deploy."),
) -> Dict[str, str]:
    """
    Deactivates and deletes a previously deployed router.
    """
    user_id = request_fastapi.state.user_id
    # Check if the router exists
    if not router_is_deployed(user_id, name):
        raise router_is_not_deployed
    # Send the request with the job to the router deployment service
    send_to_deploy_server(action="delete", user_id=user_id, name=name)
    #   un-deploy router
    #   modify entry in the db
    return {"info": "Router deletion started! You will receive an email soon!"}


@router.get(
    "/router/deploy/list",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {
                        "router_1": {
                            "dataset": "dataset_1",
                            "endpoints": ["model@provider", "..."],
                        },
                        "...": {"..."},
                    },
                },
            },
        },
    },
)
@handle_on_prem(endpoint="/router/deploy/list", method="get")
def get_deployed_routers(
    request_fastapi: Request,
) -> Dict[str, Dict[str, Union[str, List[str]]]]:
    """
    Fetches a list of the deployed routers and relevant metadata. These routers only
    include deployed routers. To fetch a list of all trained routers,
    you can use the /router/train/list GET endpoint.

    To use any of these routers, replace the model in the endpoint string with the
    router name. E.g. you can use `router-abc` with the endpoint `router-abc@q:1`.

    """
    user_id = request_fastapi.state.user_id
    routers = _list_deployed_routers(user_id)
    # TODO: Do this correctly
    routers_metadata = {}
    for router in routers:
        routers_metadata[router] = {"dataset": "", "endpoints": [""]}
    return routers_metadata
