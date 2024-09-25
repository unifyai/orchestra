"""
Includes endpoints for router training.
"""

import os
from typing import Any, Dict, List, Union

from fastapi import APIRouter, Query, Request
from google.cloud import storage

from providers.completion import PROVIDER_CLASSES

from orchestra.web.api.utils import gcp, on_prem
from orchestra.web.api.utils.gcp import (
    blob_exists,
    list_dir,
    read_from_bucket,
    send_pubsub_msg,
<<<<<<< HEAD:orchestra/web/api/routing/views.py
    vertex_ai_endpoint_exists,
    vertex_ai_endpoint_list,
    vertex_ai_endpoint_undeploy,
=======
>>>>>>> main:orchestra/web/api/router_training/views.py
)
from orchestra.web.api.utils.http_responses import (
    dataset_does_not_exist,
    invalid_training_endpoints,
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
    if os.environ.get("ON_PREM"):
        id_to_name = on_prem.internal_id_to_displayname(user_id)
    else:
        id_to_name = gcp.internal_id_to_displayname(user_id)
    name_to_id = {name: id_ for id_, name in id_to_name.items()}
    internal_id = name_to_id.get(name, name)
    blob_name = f"{user_id}/{internal_id}/0/dataset.jsonl"
    if blob_exists(bucket_name, blob_name):
        return True
    return False


def router_training_exists(user_id, name):
    # TODO: The router directory with files needs a
    # metadata.json with the datasets it has been trained on
    bucket_name = "custom_router_data"
    dr = f"custom_router/{user_id}/{name}/"
    files = ["config.yaml", "model_mapping.json", "model.pth"]
    for f in files:
        if not blob_exists(bucket_name, dr + f):
            return False
    return True


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
                metadata = {"dataset": "", "endpoints": [""]}
            router_name = b.id.split("/")[3]
            routers_metadata[router_name] = metadata
    return routers_metadata


def send_to_train_server(action, **data):
    topic = "projects/saas-368716/topics/train_router"
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
                        "info": "Router training started! "
                        "You will receive an email soon!",
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
@handle_on_prem(endpoint="/router/train", method="none")
def train_router(
    request_fastapi: Request,
    name: str = Query(description="Name of the router.", example="router1"),
    dataset: str = Query(
        description=(
            "Name of the dataset to train the router on."
            " To use a dataset, you need to first upload it to your account"
            " using the `/dataset` POST endpoints."
        ),
        example="dataset1",
    ),
    endpoints: List[str] = Query(
        description=(
            "List of endpoints to include in the router."
            " Endpoints must be specified using the `model@provider` format."
        ),
        example=[
            "gpt-4o-mini@openai",
            "claude-3.5-sonnet@anthropic",
            "llama-3.1-405b-chat@fireworks-ai",
        ],
    ),
) -> Dict[str, str]:
    """
    Train a router based on a specified training dataset and a set of endpoints to route
    across. To *use* a custom-trained router, you will need to deploy the resulting
    artifacts to a live endpoint, via the `/router/deploy` `POST` endpoint.
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


<<<<<<< HEAD:orchestra/web/api/routing/views.py
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


=======
>>>>>>> main:orchestra/web/api/router_training/views.py
@router.delete(
    "/router",
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
                            "Please, choose a different one or "
                            "trigger the training first."
                        ),
                    },
                },
            },
        },
    },
)
@handle_on_prem(endpoint="/router", method="none")
def delete_router(
    request_fastapi: Request,
    name: str = Query(
        description="Name of the router to delete.",
        example="router1",
    ),
) -> Dict[str, str]:
    """
    Deletes a specific trained router, as well as all the training files etc.
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


@router.post("/router/rename")
@handle_on_prem(endpoint="/router/rename", method="none")
def rename_router(
    name: str = Query(
        description="The original name of the router.",
        example="original_name",
    ),
    new_name: str = Query(
        description="The new name for the router.",
        example="new_name",
    ),
):
    """
    Renames the specified router from `name` to `new_name`.
    """
<<<<<<< HEAD:orchestra/web/api/routing/views.py
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
    Deactivates a previously deployed router.
    """
    user_id = request_fastapi.state.user_id
    # Check if the router exists
    if not router_is_deployed(user_id, name):
        raise router_is_not_deployed
    # Send the request with the job to the router deployment service
    # send_to_deploy_server(action="delete", user_id=user_id, name=name)
    vertex_ai_endpoint_undeploy(user_id=user_id, name=name)
    #   un-deploy router
    #   modify entry in the db
    return {"info": "Router deletion started."}
=======
    raise NotImplemented  # ToDo: implement
>>>>>>> main:orchestra/web/api/router_training/views.py


@router.get(
    "/router/list",
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
@handle_on_prem(endpoint="/router/list", method="none")
def list_routers(
    request_fastapi: Request,
) -> Dict[str, Dict[str, Union[str, List[str]]]]:
    """
    Lists all the trained routers and the relevant metadata.
    These routers are training artifacts and therefore don't imply an active,
    deployed router. To fetch a list of deployed routers, you can use the
    `/router/deploy/list` `GET` endpoint.
    """
    user_id = request_fastapi.state.user_id
<<<<<<< HEAD:orchestra/web/api/routing/views.py
    routers_metadata = _list_deployed_routers(user_id)
    trained_routers = _list_trained_routers(user_id)
    ret = {
        router_name: trained_routers[router_name]
        for router_name in sorted(routers_metadata)
    }
    return ret
=======
    routers = _list_trained_routers(user_id)
    # TODO: Do this correctly
    routers_metadata = {}
    for rtr in routers:
        routers_metadata[rtr] = {"dataset": "", "endpoints": [""]}
    return routers_metadata


@router.get("/get_dataset_evaluation")
@handle_on_prem(endpoint="/get_dataset_evaluation", method="get")
def get_dataset_evaluation(
    request_fastapi: Request,
    dataset_name: str,
) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
    """
    Retrieve specific dataset evaluation object from the database.
    """

    bucket_name = "plot-points-temp-storage"
    blob_name = f"{dataset_name}.json"
    points = read_from_bucket(bucket_name, blob_name)

    return points
>>>>>>> main:orchestra/web/api/router_training/views.py
