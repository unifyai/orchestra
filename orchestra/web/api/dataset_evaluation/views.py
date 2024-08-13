"""
Includes endpoints related to dataset evaluations.
"""

import hashlib
import json
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Query, Request, HTTPException, File, UploadFile
from google.cloud import storage

from providers.completion import PROVIDER_CLASSES
from orchestra.web.api.utils import gcp, on_prem
from orchestra.web.api.utils.http_responses import (
    dataset_does_not_exist,
    evaluation_does_not_exist,
    invalid_training_endpoints,
)
from orchestra.web.api.dataset_evaluation.schema import EvalConfig

router = APIRouter()

# utils


# TODO: Move to utils (duplicated in dataset)
def _list_datasets(user_id: str):
    bucket_name = "uploaded_datasets"
    blobs = (
        on_prem.list_dir(bucket_name, user_id)
        if os.environ.get("ON_PREM")
        else gcp.list_dir(bucket_name, user_id)
    )
    dirs = set([b.id.split("/")[2] for b in blobs])
    # Clean legacy datasets
    dirs = {d for d in dirs if not d.endswith(".jsonl")}
    if os.environ.get("ON_PREM"):
        id_to_name = on_prem.internal_id_to_displayname(user_id)
    else:
        id_to_name = gcp.internal_id_to_displayname(user_id)
    dirs = [id_to_name.get(d, d) for d in dirs]
    return list(dirs)


def _list_evaluations(user_id: str, dataset: str):
    bucket_name = "uploaded_datasets"
    if os.environ.get("ON_PREM"):
        id_to_name = on_prem.internal_id_to_displayname(user_id)
    else:
        id_to_name = gcp.internal_id_to_displayname(user_id)
    name_to_id = {name: id_ for id_, name in id_to_name.items()}
    internal_id = name_to_id.get(dataset, dataset)
    blobs = (
        on_prem.list_dir(bucket_name, f"{user_id}/{internal_id}")
        if os.environ.get("ON_PREM")
        else gcp.list_dir(bucket_name, f"{user_id}/{internal_id}")
    )
    endpoints = []
    for b in blobs:
        # keep only the endpoints
        b_id = b if os.environ.get("ON_PREM") else b.id
        levels = b_id.split("/")
        if "judgements.jsonl" in b_id and len(levels) > 4:
            endpoints.append(levels[4])
    return endpoints


def _get_scores(user_id: str, dataset: str):
    if os.environ.get("ON_PREM"):
        id_to_name = on_prem.internal_id_to_displayname(user_id)
    else:
        id_to_name = gcp.internal_id_to_displayname(user_id)
    name_to_id = {name: id_ for id_, name in id_to_name.items()}
    internal_id = name_to_id.get(dataset, dataset)
    return (
        on_prem.get_scores(user_id, internal_id)
        if os.environ.get("ON_PREM")
        else gcp.get_scores(user_id, internal_id)
    )


def _get_input_tokens(user_id: str, dataset: str):
    if os.environ.get("ON_PREM"):
        id_to_name = on_prem.internal_id_to_displayname(user_id)
    else:
        id_to_name = gcp.internal_id_to_displayname(user_id)
    name_to_id = {name: id_ for id_, name in id_to_name.items()}
    internal_id = name_to_id.get(dataset, dataset)
    return (
        on_prem.get_input_tokens(user_id, internal_id)
        if os.environ.get("ON_PREM")
        else gcp.get_input_tokens(user_id, internal_id)
    )


def _get_response_tokens(user_id: str, dataset: str, endpoint: str):
    if os.environ.get("ON_PREM"):
        id_to_name = on_prem.internal_id_to_displayname(user_id)
    else:
        id_to_name = gcp.internal_id_to_displayname(user_id)
    name_to_id = {name: id_ for id_, name in id_to_name.items()}
    internal_id = name_to_id.get(dataset, dataset)
    return (
        on_prem.get_response_tokens(user_id, internal_id, endpoint)
        if os.environ.get("ON_PREM")
        else gcp.get_response_tokens(user_id, internal_id, endpoint)
    )


# TODO: Move to utils (duplicated in routing)
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
    exists = (
        on_prem.file_exists(bucket_name, blob_name)
        if os.environ.get("ON_PREM")
        else gcp.blob_exists(bucket_name, blob_name)
    )
    if exists:
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


def _delete_evaluation(user_id: str, dataset: str, endpoint: str):
    bucket_name = "uploaded_datasets"
    # TODO: 0 will need to be accounted when introducing dynamic datasets
    if dataset == "":
        raise dataset_does_not_exist(dataset)
    if os.environ.get("ON_PREM"):
        id_to_name = on_prem.internal_id_to_displayname(user_id)
    else:
        id_to_name = gcp.internal_id_to_displayname(user_id)
    name_to_id = {name: id_ for id_, name in id_to_name.items()}
    internal_id = name_to_id.get(dataset, dataset)
    dir_name = f"{user_id}/{internal_id}/0/{endpoint}"
    exists = (
        on_prem.dir_exists(bucket_name, dir_name)
        if os.environ.get("ON_PREM")
        else gcp.dir_exists(bucket_name, dir_name)
    )
    if not exists:
        raise evaluation_does_not_exist(dataset)
    elif os.environ.get("ON_PREM"):
        on_prem.delete_dir(bucket_name, dir_name)
    else:
        gcp.delete_dir(bucket_name, dir_name)


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
            status_code=400, detail=f"You don't have an eval with the name {eval_name}."
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


@router.post("/evals/create")
def create_eval(
    request_fastapi: Request,
    request: EvalConfig,
):
    """
    Create an eval.
    """
    user_id = request_fastapi.state.user_id

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


@router.get("/evals/list_configs")
def list_evals(
    request_fastapi: Request,
):
    """
    Returns the names of the eval configurations you have created.
    """
    displayname_to_id = build_displayname_to_id(request_fastapi.state.user_id)
    return sorted(displayname_to_id.keys())


@router.get("/evals/get_config")
def return_eval_config(
    request_fastapi: Request,
    eval_name: str = Query(
        description="Name of the eval to return the configuration of"
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


@router.post("/evals/rename")
def rename_eval(
    request_fastapi: Request,
    eval_name: str = Query(description="Name of the existing eval to rename"),
    new_eval_name: str = Query(description="New name for the eval"),
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


@router.delete("/evals/delete")
def delete_eval(
    request_fastapi: Request,
    eval_name: str = Query(description="Name of the eval to delete"),
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


#
@router.post(
    "/evals/trigger",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {
                        "info": "Dataset evaluation started! You will receive an email soon!",
                    },
                },
            },
        },
        400: {
            "description": "Invalid Endpoints",
            "content": {
                "application/json": {
                    "example": {
                        "detail": (
                            "Invalid input. Couldn't find"
                            " endpoints [model_1@endpoint_1, model_2@endpoint_2]."
                        ),
                    },
                },
            },
        },
        404: {
            "description": "Dataset Not Found",
            "content": {
                "application/json": {
                    "example": {"detail": "This dataset does not exist!"},
                },
            },
        },
    },
)
def trigger_eval(
    request_fastapi: Request,
    dataset: str = Query(..., description="Name of the uploaded dataset to evaluate."),
    endpoint: str = Query(
        ...,
        description=(
            "Name of the endpoint to evaluate."
            " Endpoints must be specified using the `model@provider` format."
        ),
    ),
    eval_name: str = Query(..., description="Name of the eval to use."),
    client_side_scores: Optional[UploadFile] = File(
        default=None,
        description="Optionally upload client-side scores. The format needs to be a file in JSONL format, in the same order as the `dataset`. The keys need to be `prompt` and `score`, where `score` should be a float between 0 and 1. The eval with corresponding `eval_name` needs to have `client_side=True`."
    ),
) -> Dict[str, str]:
    """
    Uses the named `eval` to begin an evaluation of quality scores for the selected LLM `endpoint`, on the given `dataset`.
    Once the evaluation has finished, you can access the scores using the `evals/get_scores` endpoint.
    """

    user_id = request_fastapi.state.user_id
    user_email = request_fastapi.state.user_email
    api_key = request_fastapi.headers["authorization"].removeprefix("Bearer ")
    # Check if the dataset exists
    if not dataset_exists(user_id, dataset):
        raise dataset_does_not_exist(dataset)
    # Check that the endpoints are valid
    invalid_endpoints = find_invalid_endpoints([endpoint])
    if invalid_endpoints:
        raise invalid_training_endpoints(invalid_endpoints)
    if os.environ.get("ON_PREM"):
        id_to_name = on_prem.internal_id_to_displayname(user_id)
    else:
        id_to_name = gcp.internal_id_to_displayname(user_id)
    name_to_id = {name: id_ for id_, name in id_to_name.items()}
    internal_id = name_to_id.get(dataset, dataset)
    # check if the eval name is valid
    eval_id = eval_name_to_eval_id(user_id, eval_name)

    if client_side_scores:
        # TODO: check whether matches dataset 
        try:
            lines = client_side_scores.decode().split("\n")
            lines = [json.loads(l) for l in lines if l != ""]
            for ix, line in enumerate(lines):
                if line.keys() != ["prompt", "score"]:
                    raise HTTPException(status_code=400, detail=f"Error in line {ix}")
        except:
            raise HTTPException(
                status_code=400, detail="Error processing uploaded scores"
            )

        # check whether the eval name is a client side one: 
        blob = load_eval_config_blob(user_id, eval_id)
        contents = json.loads(blob.download_as_bytes().decode("utf-8"))
        if "client_side" not in contents or contents.get("client_side", "") != True:
            raise HTTPException(status_code=400, detail=f"The eval {eval_name} is not a client-side eval (as client_side != True)")


        # put everything in the bucket
        bucket_name = "uploaded_datasets"
        blob_name = f"{user_id}/{dataset}/0/{endpoint}/{eval_id}/client_side_judged.jsonl"
        blob = storage.Client().bucket(bucket_name).blob(blob_name)
        blob.upload_from_file(client_side_scores)

        return {"info": "Evaluation uploaded!"}


    # Send train job to the dataset_evaluation server
    send_to_dataset_evaluation_server(
        action="evaluate",
        user_id=user_id,
        user_email=user_email,
        api_key=api_key,
        dataset=internal_id,
        endpoint=endpoint,
        eval_id=eval_id,
    )
    return {"info": "Dataset evaluation started! You will receive an email soon!"}


@router.get(
    "/evals/get_scores",
)
def get_eval_scores(
    request_fastapi: Request,
    dataset: str = Query(
        ..., description="Name of the dataset to fetch evaluation from."
    ),
    eval_name: Optional[str] = Query(
        default=None,
        description="Name of the eval to fetch evaluation from. If `None`, returns all available evaluations for the dataset.",
    ),
    per_prompt: bool = Query(
        default=False,
        description="If `True`, returns the scores on a per-prompt level. By default set to `False`.",
    ),
) -> Dict:
    """
    Fetches the results of an eval on a given dataset. If no `eval_name` is provided, returns scores for all completed evals on that dataset.
    """
    user_id = request_fastapi.state.user_id
    if not dataset_exists(user_id, dataset):
        raise dataset_does_not_exist(dataset)

    if os.environ.get("ON_PREM"):
        id_to_name = on_prem.internal_id_to_displayname(user_id)
    else:
        id_to_name = gcp.internal_id_to_displayname(user_id)
    name_to_id = {name: id_ for id_, name in id_to_name.items()}
    internal_id = name_to_id.get(dataset, dataset)

    if per_prompt:
        raise HTTPException(status_code=501, detail="Not implemented yet")

    return_single_eval = eval_name is not None
    requested_eval_id = (
        eval_name_to_eval_id(user_id, eval_name) if return_single_eval else None
    )

    # format of scores is {eval_id: {endpoint : {judge : score}}}
    scores = _get_scores(user_id, internal_id)

    output_tokens = {}
    id_to_displayname = build_id_to_displayname(user_id)

    ret = {}
    for eval_id, eval_scores in scores.items():
        if return_single_eval and eval_id != requested_eval_id:
            continue

        displayname = id_to_displayname[eval_id]

        ret[displayname] = eval_scores
        for endpoint in eval_scores:
            output_tokens[endpoint] = _get_response_tokens(
                user_id,
                internal_id,
                endpoint,
            )

    ret["input_tokens"] = _get_input_tokens(user_id, dataset)
    ret["output_tokens"] = output_tokens

    return ret
