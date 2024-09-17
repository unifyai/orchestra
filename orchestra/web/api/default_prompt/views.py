"""
Includes endpoints related to default prompts.
"""

import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from orchestra.db.dao.default_prompt_dao import DefaultPromptDAO
from orchestra.web.api.default_prompt.schema import DefaultPromptConfig

router = APIRouter()


###########################
# endpoints
###########################


@router.post(
    "/default_prompt",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {"info": "Default Prompt created successfully!"},
                },
            },
        },
        400: {
            "description": "Invalid Default Prompt",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Incorrect format. Could not create default prompt.",
                    },
                },
            },
        },
    },
)
def create_default_prompt(
    request_fastapi: Request,
    request: DefaultPromptConfig,
    default_prompt_dao: DefaultPromptDAO = Depends(),
):
    """
    Create a re-usable, named default prompt, and adds this to your account. This can be used
    as an argument to the `/evaluation` POST endpoint.
    """

    try:
        default_prompt_dao.create(
            user_id=request_fastapi.state.user_id,
            name=request.name,
            prompt=json.dumps(request.prompt),
        )

        return {"info": "Default Prompt created successfully!"}
    except:
        raise HTTPException(
            status_code=400,
            detail="Incorrect format. Could not create default prompt.",
        )


@router.get(
    "/default_prompt",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {
                        "name": "default_prompt_1",
                        "prompt": "{...}",
                    },
                },
            },
        },
        404: {
            "description": "Default Prompt Not Found",
            "content": {
                "application/json": {
                    "detail": "Default Prompt <name> not found in your account.",
                },
            },
        },
    },
)
def get_default_prompt(
    request_fastapi: Request,
    name: str = Query(
        description="Name of the default prompt to return the configuration of.",
        example="default_prompt_1",
    ),
    default_prompt_dao: DefaultPromptDAO = Depends(),
):
    """
    Returns the name and prompt of a default prompt from your account. The configuration
    contains the same information as the arguments passed to the `POST` function of
    `/v0/default_prompt`.
    """
    raw_eval_data = default_prompt_dao.filter(
        user_id=request_fastapi.state.user_id,
        name=name,
    )
    if not raw_eval_data:
        raise HTTPException(
            status_code=404,
            detail=f"Default Prompt {name} not found in your account.",
        )
    return raw_eval_data[0]


@router.delete(
    "/default_prompt",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {"info": "Default Prompt deleted successfully!"},
                },
            },
        },
        404: {
            "description": "Default Prompt Not Found",
            "content": {
                "application/json": {
                    "detail": "Default Prompt <name> not found in your account.",
                },
            },
        },
    },
)
def delete_default_prompt(
    request_fastapi: Request,
    name: str = Query(
        description="Name of the default_prompt to delete.",
        example="default_prompt_1",
    ),
    default_prompt_dao: DefaultPromptDAO = Depends(),
):
    """
    Deletes a default prompt from your account.
    """
    try:
        default_prompt_dao.delete_default_prompt(
            user_id=request_fastapi.state.user_id,
            name=name,
        )
    except ValueError:
        raise HTTPException(
            status_code=404,
            detail=f"Default Prompt {name} not found in your account.",
        )
    return {"info": "Default Prompt deleted successfully"}


@router.post(
    "/default_prompt/rename",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {"info": "Default Prompt renamed successfully!"},
                },
            },
        },
        404: {
            "description": "Default Prompt Not Found",
            "content": {
                "application/json": {
                    "detail": "Default Prompt <name> not found in your account.",
                },
            },
        },
    },
)
def rename_default_prompt(
    request_fastapi: Request,
    name: str = Query(
        description="Name of the default prompt to rename.",
        example="default_prompt_1",
    ),
    new_name: str = Query(
        description="New name for the default prompt.",
        example="default_prompt_2",
    ),
    default_prompt_dao: DefaultPromptDAO = Depends(),
):
    """
    Renames a default prompt from `name` to `new_name` in your account.
    """
    try:
        default_prompt_dao.rename(
            user_id=request_fastapi.state.user_id,
            name=name,
            new_name=new_name,
        )
    except ValueError:
        raise HTTPException(
            status_code=404,
            detail=f"Default Prompt {name} not found in your account.",
        )
    return {"info": "Default Prompt renamed successfully!"}


@router.get(
    "/default_prompt/list",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": [
                        "default_prompt_a",
                        "default_prompt_b",
                        "default_prompt_c",
                    ],
                },
            },
        },
    },
)
def list_default_prompts(
    request_fastapi: Request,
    default_prompt_dao: DefaultPromptDAO = Depends(),
):
    """
    Returns the names of all default prompts stored in your account.
    """
    raw_default_prompts = default_prompt_dao.filter(
        user_id=request_fastapi.state.user_id,
    )
    return [dp.name for dp in raw_default_prompts]
