"""
Includes endpoints related to evaluators.
"""

import hashlib
import json
import os

from fastapi import APIRouter, HTTPException, Query, Request
from providers.completion import PROVIDER_CLASSES

from orchestra.web.api.evaluators.schema import EvaluatorConfig
from orchestra.web.api.utils import gcp, on_prem

router = APIRouter()

# utils


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
    url = "https://api.unify.ai"
    if os.environ.get("ON_PREM"):
        on_prem.send_pubsub_msg(topic, {"action": action, **data, "orchestra_url": url})
    else:
        gcp.send_pubsub_msg(topic, {"action": action, **data, "orchestra_url": url})
    print(f"Published: {str({'action': action, **data, 'orchestra_url': url})}")


def refresh_scores_json(user_id):
    send_to_dataset_evaluation_server(action="refresh_scores", user_id=user_id)


def build_id_to_displayname(user_id):
    bucket_name = "uploaded_datasets"
    id_to_displayname = {}
    prefix = f"{user_id}/evaluation_configs"
    blobs = (
        on_prem.list_dir(bucket_name, prefix)
        if os.environ.get("ON_PREM")
        else gcp.list_dir(bucket_name, prefix)
    )
    print(f"blobs {blobs}")
    for blob in blobs:
        name = blob if os.environ.get("ON_PREM") else blob.name
        if not name.endswith(".config"):
            continue
        id_ = name.split("/")[-1]
        assert ".config" in id_
        id_ = id_.replace(".config", "")
        print(f"bucket_name {bucket_name}")
        print(f"name {name}")
        # get display_name
        blob_dict = (
            on_prem.read_json_from_folder(bucket_name, name)
            if os.environ.get("ON_PREM")
            else gcp.read_json_from_bucket(bucket_name, name)
        )
        if "name" in blob_dict:
            display_name = blob_dict["name"]
        else:
            display_name = blob_dict["eval_name"]
        id_to_displayname[id_] = display_name
    return id_to_displayname


def build_displayname_to_id(user_id):
    id_to_displayname = build_id_to_displayname(user_id)
    return {v: k for k, v in id_to_displayname.items()}


def name_to_eval_id(user_id, name):
    displayname_to_id = build_displayname_to_id(user_id)
    if name not in displayname_to_id:
        raise HTTPException(
            status_code=400,
            detail=f"You don't have an eval with the name {name}.",
        )
    return displayname_to_id[name]


def check_if_name_free(user_id, name):
    displayname_to_id = build_displayname_to_id(user_id)
    if name in displayname_to_id:
        raise HTTPException(
            status_code=400,
            detail=f"You already have an eval with the name {name}!",
        )
    return True


def load_eval_config_blob(bucket_name, blob_name):
    return (
        on_prem.read_json_from_folder(bucket_name, blob_name)
        if os.environ.get("ON_PREM")
        else gcp.read_json_from_bucket(bucket_name, blob_name)
    )


def delete_eval_config_blob(bucket_name, blob_name):
    if os.environ.get("ON_PREM"):
        on_prem.delete(bucket_name, blob_name)
    else:
        gcp.delete(bucket_name, blob_name)


def rename_eval_config_blob(bucket_name, blob_name, new_name):
    contents = load_eval_config_blob(bucket_name, blob_name)
    contents["name"] = new_name
    config_str = json.dumps(contents, sort_keys=True)
    if os.environ.get("ON_PREM"):
        on_prem.write_json_to_folder(config_str, bucket_name, blob_name)
    else:
        gcp.upload_json_to_bucket(config_str, bucket_name, blob_name)


###########################
# endpoints
###########################


@router.post(
    "/evaluator",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {"info": "Evaluator created successfully!"},
                },
            },
        },
    },
)
def create_evaluator(
    request_fastapi: Request,
    request: EvaluatorConfig,
):
    """
    Create a re-usable, named evaluator, and adds this to your account. This can be used
    to trigger an evaluation via `POST` requests to the `/evaluation` endpoint.
    """
    user_id = request_fastapi.state.user_id

    judge_models = request.judge_models
    if isinstance(request.judge_models, str):
        judge_models = [request.judge_models]

    invalid_endpoints = find_invalid_endpoints(judge_models)
    if invalid_endpoints:
        raise HTTPException(
            status_code=400,
            detail=f"Could not find {'.'.join(invalid_endpoints)}"
            f"to use as a judge model.",
        )

    # create evaluation id
    eval_cfg_body = request.model_dump()
    config_str = json.dumps(eval_cfg_body, sort_keys=True)
    eval_id = hashlib.shake_128(config_str.encode("utf-8")).hexdigest(8)

    # check if name is not already in use
    name = request.name
    check_if_name_free(user_id, name)

    bucket_name = "uploaded_datasets"
    file_path = f"{user_id}/evaluation_configs/{eval_id}.config"
    if os.environ.get("ON_PREM"):
        on_prem.write_json_to_folder(config_str, bucket_name, file_path)
    else:
        gcp.upload_json_to_bucket(config_str, bucket_name, file_path)
    return {"info": "Evaluator created successfully!"}


@router.get(
    "/evaluator",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {
                        "class_config": "...",
                        "client_side": "false",
                        "eval_name": "evaluator",
                        "judge_models": "claude-3.5-sonnet@aws-bedrock",
                        "system_prompt": "...",
                    },
                },
            },
        },
    },
)
def get_evaluator(
    request_fastapi: Request,
    name: str = Query(
        description="Name of the evaluator to return the configuration of.",
        example="eval1",
    ),
):
    """
    Returns the configuration JSON for an evaluator from your account. The configuration
    contains the same information as the arguments passed to the `POST` function for the
    same endpoint `/v0/evaluator`.
    """
    user_id = request_fastapi.state.user_id
    eval_id = name_to_eval_id(user_id, name)
    bucket_name = "uploaded_datasets"
    blob_name = f"{user_id}/evaluation_configs/{eval_id}.config"
    contents = load_eval_config_blob(bucket_name, blob_name)
    return contents


@router.delete(
    "/evaluator",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {"info": "Evaluator deleted successfully!"},
                },
            },
        },
    },
)
def delete_evaluator(
    request_fastapi: Request,
    name: str = Query(description="Name of the evaluator to delete.", example="eval1"),
):
    """
    Deletes an evaluator from your account.
    """
    user_id = request_fastapi.state.user_id
    eval_id = name_to_eval_id(user_id, name)
    bucket_name = "uploaded_datasets"
    blob_name = f"{user_id}/evaluation_configs/{eval_id}.config"
    delete_eval_config_blob(bucket_name, blob_name)
    refresh_scores_json(user_id)
    return {"info": "Evaluator deleted successfully!"}
    # TODO: remove all corresponding model judgements?


@router.post(
    "/evaluator/rename",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {"info": "Evaluator renamed successfully!"},
                },
            },
        },
    },
)
def rename_evaluator(
    request_fastapi: Request,
    name: str = Query(
        description="Name of the evaluator to rename.",
        example="eval1",
    ),
    new_name: str = Query(description="New name for the evaluator.", example="eval2"),
):
    """
    Renames an evaluator from `name` to `new_name` in your account.
    """
    user_id = request_fastapi.state.user_id
    eval_id = name_to_eval_id(user_id, name)
    bucket_name = "uploaded_datasets"
    blob_name = f"{user_id}/evaluation_configs/{eval_id}.config"
    rename_eval_config_blob(bucket_name, blob_name, new_name)
    refresh_scores_json(user_id)
    return {"info": "Evaluator renamed successfully!"}


@router.get(
    "/evaluator/list",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": ["evaluator_a", "evaluator_b", "evaluator_c"],
                },
            },
        },
    },
)
def list_evaluators(
    request_fastapi: Request,
):
    """
    Returns the names of all evaluators stored in your account.
    """
    displayname_to_id = build_displayname_to_id(request_fastapi.state.user_id)
    return sorted(displayname_to_id.keys())
