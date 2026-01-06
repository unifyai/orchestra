"""
Includes endpoints for router training.
"""

import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from providers.completion import PROVIDER_CLASSES

from orchestra.db.dao.dataset_dao import DatasetDAO
from orchestra.db.dao.router_dao import RouterDAO
from orchestra.web.api.utils.gcp import read_from_bucket, send_pubsub_msg

router = APIRouter()

# utils


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


def send_to_train_server(action, **data):
    topic = "projects/saas-368716/topics/train_router"
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
def train_router(
    request_fastapi: Request,
    name: str = Query(description="Name of the router.", example="my_router"),
    dataset: Optional[str] = Query(
        default=None,
        description="Name of the uploaded dataset to train a router on. Must pass exactly one of `dataset`, `prompts`.",
        example="dataset1",
    ),
    prompts: Optional[str] = Query(
        default=None,
        description="Specify the prompts to train a router on. Pass a string of comma separated integers. Must pass exactly one of `dataset`, `prompts`.",
        example="34,89,127",
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
    dataset_dao: DatasetDAO = Depends(),
    router_dao: RouterDAO = Depends(),
) -> Dict[str, str]:
    """
    Train a router based on a specified training dataset and a set of endpoints to route
    across. To *use* a custom-trained router, you will need to deploy the resulting
    artifacts to a live endpoint, via the `/router/deploy` `POST` endpoint.
    """
    user_id = request_fastapi.state.user_id
    api_key = request_fastapi.headers["authorization"].removeprefix("Bearer ")

    # Check if the name is unique
    name_exists = router_dao.filter(user_id=user_id, name=name)
    if name_exists:
        raise HTTPException(
            status_code=400,
            detail=f"You already have a router named {name}",
        )

    """
    datum_ids = get_datum_ids(
        dataset=dataset,
        prompts=prompts,
        user_id=user_id,
        dataset_dao=dataset_dao,
        stored_prompt_dao=stored_prompt_dao,
    )

    # Check that the endpoints are valid
    invalid_endpoints = find_invalid_endpoints(endpoints)
    if invalid_endpoints:
        raise invalid_training_endpoints(invalid_endpoints)

    # check if the evaluator exists
    evaluator_id = evaluator_dao.filter(user_id=user_id, name=evaluator)
    if not evaluator_id:
        raise HTTPException(
            status_code=400,
            detail=f"You don't have an evaluator named: {evaluator}",
        )
    evaluator_id = evaluator_id[0].id

    # TODO: check the evaluations exist
    # e.g. what if all the evaluations haven't finished
    # or no evaluations exist

    # create in the router db
    router_id = router_dao.create(
        user_id=user_id,
        name=name,
        endpoints=",".join(endpoints),
        evaluator_id=evaluator_id,
    )

    # TODO: email!
    # TODO: endpoint to update on if trained
    # TODO: endpoint to update on if deployed + id etc

    # Send train job to the training server
    send_to_train_server(
        action="train",
        user_id=user_id,
        # user_email=user_email,
        api_key=api_key,
        datum_ids=datum_ids,
        router_id=router_id,
        endpoints=endpoints,
        evaluator=evaluator,
    )
    return {"info": "Router training started! You will receive an email soon!"}
    """


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
                        "detail": ("You don't have a router with the name: my_router"),
                    },
                },
            },
        },
    },
)
def delete_router(
    request_fastapi: Request,
    name: str = Query(
        description="Name of the router to delete.",
        example="my_router",
    ),
    router_dao: RouterDAO = Depends(),
) -> Dict[str, str]:
    """
    Deletes a specific trained router, as well as all the training files etc.
    """
    user_id = request_fastapi.state.user_id
    name_exists = router_dao.filter(user_id=user_id, name=name)
    if not name_exists:
        raise HTTPException(
            status_code=400,
            detail=f"You don't have a router with the name: {name}",
        )

    # TODO: delete training artifacts + from gcp

    router_dao.delete(user_id=user_id, name=name)

    return {"info": "Trained router deleted!"}


@router.post("/router/rename")
def rename_router(
    request_fastapi: Request,
    name: str = Query(
        description="The original name of the router.",
        example="original_name",
    ),
    new_name: str = Query(
        description="The new name for the router.",
        example="new_name",
    ),
    router_dao: RouterDAO = Depends(),
):
    """
    Renames the specified router from `name` to `new_name`.
    """
    user_id = request_fastapi.state.user_id

    name_exists = router_dao.filter(user_id=user_id, name=name)
    if not name_exists:
        raise HTTPException(
            status_code=400,
            detail=f"You don't have a router with the name: {name}",
        )

    new_name_exists = router_dao.filter(user_id=user_id, name=new_name)
    if new_name_exists:
        raise HTTPException(
            status_code=400,
            detail=f"A router with the name: {new_name} already exists!",
        )

    updated_router = router_dao.rename(user_id=user_id, name=name, new_name=new_name)
    return {"info": f"Trained router {name} renamed to {new_name}!"}


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
def list_routers(
    request_fastapi: Request,
    router_dao: RouterDAO = Depends(),
) -> list[str]:
    """
    Lists all the trained routers and the relevant metadata.
    These routers are training artifacts and therefore don't imply an active,
    deployed router. To fetch a list of deployed routers, you can use the
    `/router/deploy/list` `GET` endpoint.
    """
    user_id = request_fastapi.state.user_id

    raw = router_dao.filter(user_id=user_id)

    # TODO: return more information (dataset, evaluator, endpoints etc)
    routers_list = [r.name for r in raw]
    return routers_list


# old function for frontend
@router.get("/get_dataset_evaluation")
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
