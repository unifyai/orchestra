"""
Includes endpoints for router deployment.
"""
from typing import Dict, List, Union
from fastapi import APIRouter, Query, Request

from orchestra.web.api.utils.gcp import (
    blob_exists,
    send_pubsub_msg,
    vertex_ai_endpoint_exists,
    vertex_ai_endpoint_list,
)
from orchestra.web.api.utils.http_responses import (
    router_already_deployed,
    router_is_not_deployed,
    router_training_does_not_exist,
)
from orchestra.web.api.utils.on_prem import handle_on_prem

router = APIRouter()

# utils


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


def router_is_deployed(user_id, name):
    # TODO: Implement checks to ensure no one can deploy a router if
    # the name is already in use.
    endpoint = f"{user_id}_{name}"
    if vertex_ai_endpoint_exists(endpoint):
        return True
    return False


def _list_deployed_routers(user_id: str):
    router_endpoints = vertex_ai_endpoint_list()
    clean_routers = []
    for r in router_endpoints:
        if user_id in r:
            clean_routers.append(r.removeprefix(f"{user_id}_"))
    return clean_routers


def send_to_deploy_server(action, **data):
    topic = "projects/saas-368716/topics/deploy_router"
    url = "https://api.unify.ai"  # TODO: Deal with staging/test
    send_pubsub_msg(topic, {"action": action, **data, "orchestra_url": url})


# endpoints

# TODO: Allow not sending an email for the website + composed flows


@router.post(
    "/router/deploy",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {
                        "info": "Router deployment started! "
                                "You will receive an email soon!",
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
                            "Please, choose a different one or "
                            "trigger the training first."
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
    name: str = Query(
        description="Name of the router to deploy.",
        example="router1",
    ),
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
                        "info": "Router deletion started! "
                                "You will receive an email soon!",
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
def undeploy_router(
    request_fastapi: Request,
    name: str = Query(
        description="Name of the router to un-deploy.",
        example="router1",
    ),
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
def list_deployed_routers(
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
    for rtr in routers:
        routers_metadata[rtr] = {"dataset": "", "endpoints": [""]}
    return routers_metadata
