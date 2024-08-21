"""
Includes endpoints related to dataset evaluations.
"""
import os
import json
from typing import Dict
from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from google.cloud import storage

from providers.completion import PROVIDER_CLASSES
from orchestra.web.api.utils import gcp, on_prem
from orchestra.web.api.utils.http_responses import (
    dataset_does_not_exist,
    invalid_training_endpoints,
)

router = APIRouter()

# utils


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


def load_eval_config_blob(user_id, eval_id):
    bucket_name = "uploaded_datasets"
    blob_name = f"{user_id}/evaluation_configs/{eval_id}.config"
    blob = storage.Client().bucket(bucket_name).blob(blob_name)
    return blob


###########################
# endpoints
###########################

@router.post(
    "/evaluation",
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
def trigger_evaluation(
    request_fastapi: Request,
    eval_name: str = Query(
        description="Name of the eval to use.",
        example="eval1",
    ),
    dataset: str = Query(
        description="Name of the uploaded dataset to evaluate.",
        example="dataset1",
    ),
    endpoint: str = Query(
        description=(
            "Name of the endpoint to evaluate."
            " Endpoints must be specified using the `model@provider` format."
        ),
        example="gpt-4o-mini@openai",
    ),
    client_side_scores: UploadFile = File(
        default=None,
        description="An optional file upload for client-side scores. The file must be in JSONL format and the prompts must match the order of the `dataset`. "
        "Each entry should include `prompt` and `score` keys, with `score` being a float between 0 and 1. The evaluation corresponding to the `eval_name` must have `client_side=True`.",
        json_schema_extra={"example": "client_scores.jsonl"},
    ),
) -> Dict[str, str]:
    """
    Uses the named `evaluator` to trigger an evaluation of quality scores for the
    selected LLM `endpoint` on the selected `dataset`. You can upload custom scores (and
    bypass the LLM judge entirely) by uploading a file via the `client_side_scores`
    argument. Once the evaluation has finished, you can access the scores using the
    `/v0/evaluation` endpoint.
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
        file = client_side_scores.file.read()
        # TODO: check whether matches dataset
        try:
            lines = file.decode().split("\n")
            lines = [json.loads(l) for l in lines if l != ""]
            for ix, line in enumerate(lines):
                if set(line.keys()) != set(["prompt", "score"]):
                    raise HTTPException(status_code=400, detail=f"Error in line {ix}")
        except:
            raise HTTPException(
                status_code=400,
                detail="Error processing uploaded scores",
            )

        # check whether the eval name is a client side one:
        blob = load_eval_config_blob(user_id, eval_id)
        contents = json.loads(blob.download_as_bytes().decode("utf-8"))
        if "client_side" not in contents or contents.get("client_side", "") != True:
            raise HTTPException(
                status_code=400,
                detail=f"The eval {eval_name} is not a client-side eval (as client_side != True)",
            )

        # put everything in the bucket
        bucket_name = "uploaded_datasets"
        blob_name = (
            f"{user_id}/{internal_id}/0/{endpoint}/{eval_id}/client_side_judged.jsonl"
        )
        blob = storage.Client().bucket(bucket_name).blob(blob_name)
        blob.upload_from_string(file, content_type="application/octet-stream")
        refresh_scores_json(user_id)

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
    "/evaluation",
)
def get_evaluations(
    request_fastapi: Request,
    dataset: str = Query(
        description="Name of the dataset to fetch evaluation from.",
        example="dataset1",
    ),
    endpoint: str = Query(
        default=None,
        description="The endpoint to fetch the evaluation for. "
                    "If `None`, returns evaluations for all endpoints.",
        example="gpt-4o-mini@openai",
    ),
    evaluator: str = Query(
        default=None,
        description="Name of the evaluator to fetch the evaluation for. "
                    "If `None`, returns all available evaluations for the dataset and "
                    "endpoint pair.",
        example="eval1",
    ),
    per_prompt: bool = Query(
        default=False,
        description="If `True`, returns the scores on a per-prompt level. "
                    "By default set to `False`.",
        example=False,
    ),
) -> Dict:
    """
    Fetches evaluation results on a given dataset, for a specific endpoint (optional)
    based on a specific evaluator (optional). If no `evaluator` is provided, then scores
    are returned for all valid evaluators. Similarly, if no `endpoint` is provided, then
    scores are returned for all valid endpoints.
    """
    # ToDo: implement the logic where the endpoint (required) is considered in the input
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

    return_single_eval = evaluator is not None
    requested_eval_id = (
        eval_name_to_eval_id(user_id, evaluator) if return_single_eval else None
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

        if endpoint:
            eval_scores = {endpoint: eval_scores[endpoint]}

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


@router.delete(
    "/evaluation",
)
def delete_evaluations(
    request_fastapi: Request,
    dataset: str = Query(
        description="Name of the dataset to delete the evaluation for.",
        example="dataset1",
    ),
    endpoint: str = Query(
        default=None,
        description="The endpoint to delete the evaluation for. "
                    "If `None`, deletes the evaluations for all endpoints.",
        example="gpt-4o-mini@openai",
    ),
    evaluator: str = Query(
        default=None,
        description="Name of the evaluator to delete the evaluation for. "
                    "If `None`, deletes all available evaluations for the dataset and "
                    "endpoint pair.",
        example="eval1",
    ),
):
    """
    Deletes evaluations on a given dataset, for a specific endpoint (optional) based on
    a specific evaluator (optional). If no `evaluator` is provided, then evaluations for
    all valid evaluators are deleted. Similarly, if no `endpoint` is provided, then
    evaluations for all valid endpoints are deleted.
    """
    raise NotImplemented
