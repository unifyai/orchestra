"""
Includes endpoints related to dataset evaluations.
"""
import os
import json
import hashlib

from fastapi import APIRouter, HTTPException, Query, Request
from google.cloud import storage
from providers.completion import PROVIDER_CLASSES

from orchestra.web.api.evaluators.schema import EvalConfig
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
    bucket = storage.Client().bucket(bucket_name)
    id_to_displayname = {}
    for blob in bucket.list_blobs(prefix=f"{user_id}/evaluation_configs"):
        if not blob.name.endswith(".config"):
            continue
        id_ = blob.name.split("/")[-1]
        assert ".config" in id_
        id_ = id_.replace(".config", "")
        # get display_name
        display_name = json.loads(blob.download_as_bytes().decode("utf-8"))["eval_name"]
        id_to_displayname[id_] = display_name
    return id_to_displayname


def build_displayname_to_id(user_id):
    id_to_displayname = build_id_to_displayname(user_id)
    return {v: k for k, v in id_to_displayname.items()}


def eval_name_to_eval_id(user_id, eval_name):
    displayname_to_id = build_displayname_to_id(user_id)
    if eval_name not in displayname_to_id:
        raise HTTPException(
            status_code=400,
            detail=f"You don't have an eval with the name {eval_name}.",
        )
    return displayname_to_id[eval_name]


def check_if_eval_name_free(user_id, eval_name):
    displayname_to_id = build_displayname_to_id(user_id)
    if eval_name in displayname_to_id:
        raise HTTPException(
            status_code=400,
            detail=f"You already have an eval with the name {eval_name}!",
        )
    return True


def load_eval_config_blob(user_id, eval_id):
    bucket_name = "uploaded_datasets"
    blob_name = f"{user_id}/evaluation_configs/{eval_id}.config"
    blob = storage.Client().bucket(bucket_name).blob(blob_name)
    return blob


###########################
# endpoints
###########################


@router.post("/evaluator/create")
def create_evaluator(
    request_fastapi: Request,
    request: EvalConfig,
):
    """
    Create a re-usable, named evaluator.
    This can be used to trigger an evaluation via the `/evals/trigger` endpoint.
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
    eval_name = request.eval_name
    check_if_eval_name_free(user_id, eval_name)

    blob = load_eval_config_blob(user_id, eval_id)
    blob.upload_from_string(config_str, content_type="application/json")
    return {"info": "Eval created!"}


@router.get("/evaluator/list")
def list_evaluators(
    request_fastapi: Request,
):
    """
    Returns the names of the eval configurations you have created.
    """
    displayname_to_id = build_displayname_to_id(request_fastapi.state.user_id)
    return sorted(displayname_to_id.keys())


@router.get("/evaluator")
def get_evaluator(
    request_fastapi: Request,
    eval_name: str = Query(
        description="Name of the eval to return the configuration of",
        example="eval1",
    ),
):
    """
    Returns the configuration JSON for a named eval.
    """
    user_id = request_fastapi.state.user_id
    eval_id = eval_name_to_eval_id(user_id, eval_name)
    blob = load_eval_config_blob(user_id, eval_id)
    contents = json.loads(blob.download_as_bytes().decode("utf-8"))
    return contents


@router.post("/evaluator/rename")
def rename_evaluator(
    request_fastapi: Request,
    eval_name: str = Query(
        description="Name of the existing eval to rename",
        example="eval1",
    ),
    new_eval_name: str = Query(description="New name for the eval", example="eval2"),
):
    """
    Renames a named eval from `eval_name` to `new_eval_name`.
    """
    user_id = request_fastapi.state.user_id
    eval_id = eval_name_to_eval_id(user_id, eval_name)
    blob = load_eval_config_blob(user_id, eval_id)
    contents = json.loads(blob.download_as_bytes().decode("utf-8"))
    contents["eval_name"] = new_eval_name
    config_str = json.dumps(contents, sort_keys=True)
    blob.upload_from_string(config_str, content_type="application/json")
    refresh_scores_json(user_id)
    return {"info": "Evaluation successfully renamed"}


@router.delete("/evaluator/delete")
def delete_evaluator(
    request_fastapi: Request,
    eval_name: str = Query(description="Name of the eval to delete", example="eval1"),
):
    """
    Deletes a named eval from your account.
    """
    user_id = request_fastapi.state.user_id
    eval_id = eval_name_to_eval_id(user_id, eval_name)
    blob = load_eval_config_blob(user_id, eval_id)
    blob.delete()
    refresh_scores_json(user_id)
    # TODO: remove all corresponding model judgements?
