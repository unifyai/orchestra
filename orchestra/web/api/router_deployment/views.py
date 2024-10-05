"""
Includes endpoints for router deployment.
"""
import os
from typing import Dict

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from orchestra.db.dao.router_dao import RouterDAO
from orchestra.web.api.utils.gcp import blob_exists, send_pubsub_msg
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


def send_to_deploy_server(action, **data):
    topic = "projects/saas-368716/topics/deploy_router"
    url = "https://api.unify.ai"  # TODO: Deal with staging/test
    send_pubsub_msg(
        topic,
        {
            "action": action,
            **data,
            "orchestra_url": url,
            "admin_key": os.environ.get("ORCHESTRA_ADMIN_KEY"),
        },
    )


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
    router_dao: RouterDAO = Depends(),
) -> Dict[str, str]:
    """
    Deploys a trained router to a live endpoint.

    To use this router, replace the model in the endpoint string with the
    router name. E.g. you can use a router named `test_router` by calling the
    `router_test_router@q:1` endpoint.

    """
    user_id = request_fastapi.state.user_id
    router_exists = router_dao.filter(user_id=user_id, name=name)

    if not router_exists:
        raise HTTPException(
            status_code=400,
            detail=f"You don't have a router with the name: {name}",
        )
    router_info = router_exists[0]
    if router_info.deployed:
        if router_info.gcp_router_id:
            raise HTTPException(
                status_code=400,
                detail=f"The router: {name} is already deployed.",
            )
        raise HTTPException(
            status_code=400,
            detail=f"The router: {name} is being deployed, please check back later.",
        )

    if not router_info.trained:
        raise HTTPException(
            status_code=400,
            detail=f"The router: {name} has not finished training.",
        )

    # Send the request with the job to the router deployment service

    # TODO: move the deployed things elsewhere
    router_dao.update(router_info.id, deployed=True)

    send_to_deploy_server(action="deploy", user_id=user_id, router_id=router_info.id)
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
    router_dao: RouterDAO = Depends(),
) -> Dict[str, str]:
    """
    Deactivates and deletes a previously deployed router, but keeps the training
    artifacts for this router, such that it can be redeployed if desired without needing
    to retrain.
    """

    user_id = request_fastapi.state.user_id
    router_exists = router_dao.filter(user_id=user_id, name=name)

    if not router_exists:
        raise HTTPException(
            status_code=400,
            detail=f"You don't have a router with the name: {name}",
        )
    router_info = router_exists[0]
    if not router_info.deployed:
        raise HTTPException(
            status_code=400,
            detail=f"The router: {name} is not deployed.",
        )

    router_dao.update(router_info.id, deployed=False)
    send_to_deploy_server(
        action="undeploy",
        user_id=user_id,
        gcp_router_id=router_info.gcp_router_id,
    )
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
    router_dao: RouterDAO = Depends(),
) -> list:
    """
    Fetches a list of the *deployed* routers and relevant metadata (excluding the
    trained but undeployed routers). To fetch a list of *all* trained routers (both
    deployed and undeployed), you can use the `/router/list` `GET` endpoint.

    To use any of these routers, replace the model in the endpoint string with the
    router name. E.g. you can use `router-abc` with the endpoint `router-abc@q:1`.

    """
    user_id = request_fastapi.state.user_id
    raw = router_dao.filter(user_id=user_id)
    # TODO: return more information (dataset, evaluator, endpoints etc)
    routers_list = [r.name for r in raw if r.deployed]
    return routers_list
