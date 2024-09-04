"""
Includes endpoints related to evaluators.
"""

import json

from fastapi import APIRouter, HTTPException, Query, Request, Depends

from providers.completion import PROVIDER_CLASSES
from orchestra.web.api.evaluators.schema import EvaluatorConfig
from orchestra.db.dao.evaluator_dao import EvaluatorDAO

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
    evaluator_dao: EvaluatorDAO = Depends(),
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

    # TODO: put these defaults somewhere sensible
    system_prompt = request.system_prompt
    if system_prompt is None:
        system_prompt = """[System]
Please act as an impartial judge and evaluate the quality of the response provided by an assistant to the user question displayed below.
Your job is to evaluate how good the assistant's answer is.
Your evaluation should consider correctness and helpfulness. Identify any mistakes.

Be as objective as possible."""

    class_config = request.class_config
    if class_config is None:
        class_config = [
            {"label": "excellent", "score": 1.0},
            {"label": "very_good", "score": 0.8},
            {"label": "good", "score": 0.5},
            {"label": "bad", "score": 0.0},
            {"label": "irrelevant", "score": 0.0},
        ]
    try:
        evaluator_dao.create(
            user_id=user_id,
            name=request.name,
            system_prompt=system_prompt,
            class_config=json.dumps(class_config),
            judge_models=judge_models,
            client_side=request.client_side,
        )

        return {"info": "Evaluator created successfully!"}
    except:
        return {"info": "Could not create evaluator, please check the format"}


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
    evaluator_dao: EvaluatorDAO = Depends(),
):
    """
    Returns the configuration JSON for an evaluator from your account. The configuration
    contains the same information as the arguments passed to the `POST` function for the
    same endpoint `/v0/evaluator`.
    """
    raw_eval_data = evaluator_dao.filter(
        user_id=request_fastapi.state.user_id, name=name
    )[0]
    return raw_eval_data


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
    evaluator_dao: EvaluatorDAO = Depends(),
):
    """
    Deletes an evaluator from your account.
    """
    return evaluator_dao.delete_evaluator(
        user_id=request_fastapi.state.user_id, name=name
    )


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
    evaluator_dao: EvaluatorDAO = Depends(),
):
    """
    Renames an evaluator from `name` to `new_name` in your account.
    """
    evaluator_dao.rename(
        user_id=request_fastapi.state.user_id, name=name, new_name=new_name
    )
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
    evaluator_dao: EvaluatorDAO = Depends(),
):
    """
    Returns the names of all evaluators stored in your account.
    """
    raw_evaluators = evaluator_dao.filter(user_id=request_fastapi.state.user_id)
    return [e.name for e in raw_evaluators]
