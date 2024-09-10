"""
Includes endpoints related to dataset evaluations.
"""

import json
import os
from typing import Dict

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile, Depends
from google.cloud import storage
from providers.completion import PROVIDER_CLASSES

from orchestra.web.api.utils import gcp, on_prem
from orchestra.web.api.utils.http_responses import (
    dataset_does_not_exist,
    invalid_training_endpoints,
)

from orchestra.db.dao.dataset_dao import DatasetDAO
from orchestra.db.dao.evaluator_dao import EvaluatorDAO
from orchestra.db.dao.evaluation_dao import EvaluationDAO
from orchestra.db.dao.stored_prompt_dao import StoredPromptDAO
from orchestra.db.dao.stored_prompt_response_dao import StoredPromptResponseDAO
from orchestra.db.dao.judgement_dao import JudgementDAO

router = APIRouter()
admin_router = APIRouter()

# utils


# TODO: Move to utils (duplicated in routing)
def dataset_exists(dataset_dao, user_id, name):
    raw_datasets = dataset_dao.filter(user_id=user_id, name=name)
    if raw_datasets:
        return raw_datasets[0].id
    return False


def get_dataset_id(dataset_dao, user_id, name):
    raw_datasets = dataset_dao.filter(user_id=user_id, name=name)
    if raw_datasets:
        return raw_datasets[0].id
    return None


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
    if os.getenv("STAGING"):
        topic = "projects/saas-368716/topics/staging_dataset_evaluation"
        url = "https://orchestra-staging-lz5fmz6i7q-ew.a.run.app"
    if os.environ.get("ON_PREM"):
        on_prem.send_pubsub_msg(topic, {"action": action, **data, "orchestra_url": url})
    else:
        gcp.send_pubsub_msg(topic, {"action": action, **data, "orchestra_url": url})
    print(f"Published: {str({'action': action, **data, 'orchestra_url': url})}")


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
                        "info": "Dataset evaluation started! "
                        "You will receive an email soon!",
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
    evaluator: str = Query(
        description="Name of the evaluator to use.",
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
        "Each entry should include `prompt` and `score` keys, with `score` being a float between 0 and 1. The evaluation corresponding to the `evaluator` must have `client_side=True`.",
        json_schema_extra={"example": "client_scores.jsonl"},
    ),
    dataset_dao: DatasetDAO = Depends(),
    evaluator_dao: EvaluatorDAO = Depends(),
    evaluation_dao: EvaluationDAO = Depends(),
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

    # Check that the endpoints are valid
    invalid_endpoints = find_invalid_endpoints([endpoint])
    if invalid_endpoints:
        raise invalid_training_endpoints(invalid_endpoints)

    # dataset_id
    dataset_id = get_dataset_id(dataset_dao, user_id, dataset)
    if dataset_id is None:
        raise dataset_does_not_exist(dataset)

    # evaluator_id
    raw_evaluators = evaluator_dao.filter(user_id=user_id, name=evaluator)
    if not raw_evaluators:
        raise HTTPException(
            400, detail=f"The evaluator {evaluator} does not exist in your account"
        )
    evaluator_id = raw_evaluators[0].id

    if client_side_scores:
        #
        raise NotImplementedError
        # file = client_side_scores.file.read()
        # # TODO: check whether matches dataset
        # try:
        #     lines = file.decode().split("\n")
        #     lines = [json.loads(l) for l in lines if l != ""]
        #     for ix, line in enumerate(lines):
        #         if set(line.keys()) != set(["prompt", "score"]):
        #             raise HTTPException(status_code=400, detail=f"Error in line {ix}")
        # except:
        #     raise HTTPException(
        #         status_code=400,
        #         detail="Error processing uploaded scores",
        #     )

        # # check whether the eval name is a client side one:
        # blob = load_eval_config_blob(user_id, eval_id)
        # contents = json.loads(blob.download_as_bytes().decode("utf-8"))
        # if "client_side" not in contents or contents.get("client_side", "") is not True:
        #     raise HTTPException(
        #         status_code=400,
        #         detail=f"The evaluator {evaluator} is not a client-side evaluator "
        #         f"(as client_side != True)",
        #     )

        # # put everything in the bucket
        # bucket_name = "uploaded_datasets"
        # blob_name = (
        #     f"{user_id}/{internal_id}/0/{endpoint}/{eval_id}/client_side_judged.jsonl"
        # )
        # blob = storage.Client().bucket(bucket_name).blob(blob_name)
        # blob.upload_from_string(file, content_type="application/octet-stream")
        # refresh_scores_json(user_id)

        # return {"info": "Evaluation uploaded!"}

    # Send train job to the dataset_evaluation server
    send_to_dataset_evaluation_server(
        action="evaluate",
        user_id=user_id,
        user_email=user_email,
        api_key=api_key,
        dataset=dataset,
        endpoint=endpoint,
        evaluator=evaluator,
        evaluator_id=evaluator_id,
    )
    return {"info": "Dataset evaluation started! You will receive an email soon!"}


@admin_router.post("/evals/admin_trigger")
@on_prem.handle_on_prem("/evals/admin_trigger", "none")
def admin_trigger_eval(
    request_fastapi: Request,
    user_id: str = Query(
        ...,
        description="ID of the user that will own the triggered eval.",
        example="clb5hxxxxxxxxx601hooxp3ct",
    ),
    name: str = Query(
        ...,
        description="Name of the eval to use.",
        example="eval1",
    ),
    dataset: str = Query(
        ...,
        description="Name of the uploaded dataset to evaluate.",
        example="dataset1",
    ),
    endpoint: str = Query(
        ...,
        description=(
            "Name of the endpoint to evaluate."
            " Endpoints must be specified using the `model@provider` format."
        ),
        example="gpt-4o-mini@openai",
    ),
) -> Dict[str, str]:
    """
    Behaves like the user-specific endpoint but can be triggered as an admin on behalf of a given user.
    """

    raise NotImplementedError

    api_key = os.getenv("UNIFY_API_KEY")

    # Check if the dataset exists
    if not dataset_exists(user_id, dataset):
        raise dataset_does_not_exist(dataset)

    # Check that the endpoints are valid
    invalid_endpoints = find_invalid_endpoints([endpoint])
    if invalid_endpoints:
        raise invalid_training_endpoints(invalid_endpoints)
    id_to_name = (
        on_prem.internal_id_to_displayname(user_id)
        if os.environ.get("ON_PREM")
        else gcp.internal_id_to_displayname(user_id)
    )
    name_to_id = {name: id_ for id_, name in id_to_name.items()}
    internal_id = name_to_id.get(dataset, dataset)
    # check if the eval name is valid
    eval_id = eval_name_to_eval_id(user_id, name)

    # Send train job to the dataset_evaluation server
    send_to_dataset_evaluation_server(
        action="evaluate",
        user_id=user_id,
        user_email="",
        api_key=api_key,
        dataset=internal_id,
        endpoint=endpoint,
        eval_id=eval_id,
    )
    return {"info": "Dataset evaluation started!"}


def get_single_evaluation(
    user_id: str,
    dataset: str,
    endpoint: str,
    evaluator: str,
    dataset_dao: DatasetDAO,
    evaluator_dao: EvaluatorDAO,
    evaluation_dao: EvaluationDAO,
    per_prompt: bool,
):
    """Get the score for one endpoint + evaluator + dataset (optionally per_prompt)"""
    dataset_prompts = dataset_dao.fetch_dataset(user_id=user_id, name=dataset)

    prompt_ids = [prompt["id"] for prompt in dataset_prompts]
    evaluator_id = evaluator_dao.filter(user_id=user_id, name=evaluator)[0].id
    scores = evaluation_dao.fetch_evaluation_scores(
        prompt_ids=prompt_ids, evaluator_id=evaluator_id, endpoint_str=endpoint
    )
    mean_score = 100 * sum(float(s.score) for s in scores) / len(scores)
    progress = 100 * len(scores) / len(prompt_ids)
    result = {"score": mean_score, "progress": progress}
    if per_prompt:
        per_prompt_scores = [{"id": _s.id, "score": float(_s.score)} for _s in scores]
        result["per_prompt"] = per_prompt_scores
    return result


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
        "If `None`, returns all available evaluations for the dataset and evaluator pair.",
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
        "By default set to `False`. If `True` requires an endpoint "
        "and evaluator to be set.",
        example=False,
    ),
    dataset_dao: DatasetDAO = Depends(),
    evaluator_dao: EvaluatorDAO = Depends(),
    evaluation_dao: EvaluationDAO = Depends(),
) -> Dict:
    """
    Fetches evaluation results on a given dataset, for a specific endpoint (optional)
    based on a specific evaluator (optional). If no `evaluator` is provided, then scores
    are returned for all valid evaluators. Similarly, if no `endpoint` is provided, then
    scores are returned for all valid endpoints.
    """
    # ToDo: implement the logic where the endpoint (required) is considered in the input
    user_id = request_fastapi.state.user_id
    if not dataset_exists(dataset_dao, user_id, dataset):
        raise dataset_does_not_exist(dataset)

    if not endpoint and not evaluator:
        raise HTTPException(
            status_code=404,
            detail="You need to specify at least one of (endpoint, evaluator)",
        )

    if per_prompt:
        if not endpoint or not evaluator:
            raise HTTPException(
                status_code=404,
                detail="If per_prompt=True, need to specify both endpoint and evaluator",
            )

    if evaluator:
        # TODO: add logic to check it's valid
        evaluators = [evaluator]
    else:
        raise NotImplementedError
        # find the evaluators in evaluations table
        evaluators = evaluation_dao.find_evaluators(
            user_id=user_id, dataset=dataset, endpoint_str=endpoint
        )
        # TODO:

    if endpoint:
        invalid_endpoints = find_invalid_endpoints([endpoint])
        if invalid_endpoints:
            raise HTTPException(
                status_code=400,
                detail=f"Could not find endpoint: {'.'.join(invalid_endpoints)}",
            )
        endpoints = [endpoint]
    else:
        raise NotImplementedError
        # find the endpoints in evaluations table
        endpoints = ...

    #### TODO: properly handle multiple endpoint,evaluator things
    # multiple judges
    # exception handling

    ret = {}
    for endpoint in endpoints:
        for evaluator in evaluators:
            eval_result = get_single_evaluation(
                user_id=user_id,
                dataset=dataset,
                endpoint=endpoint,
                evaluator=evaluator,
                dataset_dao=dataset_dao,
                evaluator_dao=evaluator_dao,
                evaluation_dao=evaluation_dao,
                per_prompt=per_prompt,
            )
            if evaluator in ret:
                ret[evaluator][endpoint] = eval_result
            else:
                ret[evaluator] = {endpoint: (eval_result)}

    return ret


@router.delete(
    "/evaluation",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {"info": "Evaluation deleted successfully!"},
                },
            },
        },
    },
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


@admin_router.post("/evaluations/upload_responses")
def upload_responses(
    request_fastapi: Request,
    prompt_id: int,
    endpoint_str: str,
    response: str,
    num_tokens: int,
    stored_prompt_response_dao: StoredPromptResponseDAO = Depends(),
):
    stored_prompt_response_dao.create(
        prompt_id=prompt_id,
        endpoint_str=endpoint_str,
        response=response,
        num_tokens=num_tokens,
    )


@admin_router.post("/evaluations/upload_judgements")
def upload_judgements(
    request_fastapi: Request,
    prompt_id: int,
    endpoint_str: str,
    evaluator_id: str,
    judge_endpoint_str: str,
    judgement: str,
    score: str,
    stored_prompt_response_dao: StoredPromptResponseDAO = Depends(),
    judgement_dao: JudgementDAO = Depends(),
    evaluation_dao: EvaluationDAO = Depends(),
):

    try:
        raw_ids = stored_prompt_response_dao.filter(
            prompt_id=prompt_id, endpoint_str=endpoint_str
        )
        response_id = raw_ids[0].id
    except Exception as e:
        raise e

    judgement_dao.create(
        response_id=response_id,
        judge_endpoint_str=judge_endpoint_str,
        evaluator_id=evaluator_id,
        judgement=judgement,
    )
    evaluation_dao.create(
        prompt_id=prompt_id,
        evaluator_id=evaluator_id,
        endpoint_str=endpoint_str,
        score=score,
    )


@admin_router.get("/dataset/load_prompt")
def load_prompt(
    request_fastapi: Request,
    prompt_id: str,
    stored_prompt_dao: StoredPromptDAO = Depends(),
):
    ret = stored_prompt_dao.filter(id=prompt_id)
    return ret


@admin_router.get("/dataset/load_response")
def load_prompt(
    request_fastapi: Request,
    prompt_id: str,
    endpoint_str: str,
    stored_prompt_response_dao: StoredPromptResponseDAO = Depends(),
):
    ret = stored_prompt_response_dao.filter(id=prompt_id, endpoint_str=endpoint_str)
    return ret

@admin_router.get("/dataset/load_judgement")
def load_judgement(
    request_fastapi: Request,
    prompt_id: str,
    endpoint_str: str,
    evaluator_id,
    evaluation_dao: EvaluationDAO = Depends(),
):
    ret = evaluation_dao.filter(prompt_id=prompt_id, evaluator_id=evaluator_id, endpoint_str=endpoint_str)
    return ret