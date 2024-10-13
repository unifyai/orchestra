from typing import Dict, List

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.param_functions import Depends
from starlette import status

from orchestra.db.dao.custom_api_key_dao import CustomApiKeyDAO
from orchestra.db.dao.custom_endpoint_dao import CustomEndpoint, CustomEndpointDAO
from orchestra.web.api.custom_endpoints.schema import CustomEndpointModelResponse
from orchestra.web.api.utils.http_responses import not_found

router = APIRouter()

VALID_CUSTOM_PROVIDERS = (
    "custom",
    "custom-openai",
    "custom-mistral",
    "custom-vertex-ai",
    "custom-fireworks-ai",
    "custom-together-ai",
)


@router.post(
    "/custom_endpoint",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {"info": "Custom endpoint created successfully!"},
                },
            },
        },
        404: {
            "description": "Custom API Key Not Found",
            "content": {
                "application/json": {
                    "example": {"detail": "Custom API Key not found."},
                },
            },
        },
    },
)
def create_custom_endpoint(
    request_fastapi: Request,
    name: str = Query(
        description="The endpoint name for your custom endpoint, "
        "in model@provider format. If it's a custom endpoint following the OpenAI "
        "format then the provider must be `@custom`, otherwise if it's a fine-tuned "
        "model from one of the existing providers it can be specified with a "
        "prepending `custom-`, i.e. `@custom-anthropic`.",
        example="endpoint1",
    ),
    url: str = Query(
        description="Base URL of the endpoint being called. "
        "Must support the OpenAI format.",
        example="https://api.url1.com",
    ),
    key_name: str = Query(
        description="Name of the API key that will be passed as part of the query.",
        example="key1",
    ),
    model_arg: str = Query(
        None,
        description=(
            "The value passed to the model arugment of the *underlying* API which is "
            "being wrapped into Unify. For example, you might call your endpoint "
            "`llama-3-baseten@custom` to distinguish the custom endpoint within Unify, "
            "but under the hood need to pass `llama-3.2-90b-chat` to the Baseten "
            "endpoint."
        ),
        example="llama-3.1-8b-finetuned",
    ),
    custom_endpoint_dao: CustomEndpointDAO = Depends(),
    custom_api_key_dao: CustomApiKeyDAO = Depends(),
) -> Dict[str, str]:
    """
    Creates a custom endpoint. This endpoint must either be a fine-tuned model from one
    of the supported providers (`/v0/providers`), in which case the "provider" argument
    must be set accordingly. Otherwise, the endpoint must support the OpenAI
    `/chat/completions` format. To query your custom endpoint, replace your endpoint
    string with `<endpoint_name>@custom` when querying any general custom endpoint. You
    can show all *custom* endpoints by querying `/v0/endpoints` and passing `custom` as
    the `provider` argument.

    """
    user_id = request_fastapi.state.user_id
    if "@" not in name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid endpoint name {name}. It must be in the format "
            "of `model@provider`, but no `@` symbol was included in the "
            "specified name.",
        )
    model, provider = name.split("@")
    if provider not in VALID_CUSTOM_PROVIDERS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid provider. Custom provider must be one of "
            f"{VALID_CUSTOM_PROVIDERS}, but found {provider}.",
        )
    try:
        key_id = custom_api_key_dao.filter(user_id=user_id, key=key_name)[0].id
    except Exception:
        raise not_found("Custom API Key")
    custom_endpoint_dao.create_custom_endpoint(
        user_id=user_id,
        name=name,
        model_arg=model_arg if model_arg else model,
        url=url,
        key_id=key_id,
    )
    return {"info": "Custom endpoint created successfully!"}


@router.delete(
    "/custom_endpoint",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {"info": "Custom endpoint deleted successfully!"},
                },
            },
        },
        404: {
            "description": "Custom Endpoint Not Found",
            "content": {
                "application/json": {
                    "example": {"detail": "Custom endpoint not found."},
                },
            },
        },
    },
)
def delete_custom_endpoint(
    request_fastapi: Request,
    name: str = Query(
        description="Name of the custom endpoint to delete.",
        example="endpoint1",
    ),
    custom_endpoint_dao: CustomEndpointDAO = Depends(),
) -> Dict[str, str]:
    """
    Deletes a custom endpoint from your account.

    """
    user_id = request_fastapi.state.user_id

    existing_endpoint = custom_endpoint_dao.filter(user_id=user_id, name=name)
    if not existing_endpoint:
        raise not_found("Custom endpoint")

    custom_endpoint_dao.delete(
        user_id=user_id,
        name=name,
    )
    return {"info": "Custom endpoint deleted successfully!"}


@router.post(
    "/custom_endpoint/rename",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {"info": "Custom endpoint renamed successfully!"},
                },
            },
        },
        404: {
            "description": "Custom endpoint Not Found",
            "content": {
                "application/json": {
                    "example": {"detail": "Custom endpoint not found."},
                },
            },
        },
    },
)
def rename_custom_endpoint(
    request_fastapi: Request,
    name: str = Query(
        description="Name of the custom endpoint to be updated.",
        example="name1",
    ),
    new_name: str = Query(
        description="New name for the custom endpoint.",
        example="name2",
    ),
    custom_endpoint_dao: CustomEndpointDAO = Depends(),
) -> Dict[str, str]:
    """
    Renames a custom endpoint in your account.

    """
    user_id = request_fastapi.state.user_id

    existing_endpoint = custom_endpoint_dao.filter(user_id=user_id, name=name)
    if not existing_endpoint:
        raise not_found("Custom endpoint")

    custom_endpoint_dao.rename(
        user_id=user_id,
        name=name,
        new_name=new_name,
    )
    return {"info": "Custom endpoint renamed successfully!"}


@router.get(
    "/custom_endpoint/list",
    response_model=List[CustomEndpointModelResponse],
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": [
                        {
                            "name": "endpoint_1",
                            "model_arg": "llama_finetune",
                            "url": "https://...",
                            "key": "custom_key_1",
                        },
                        {
                            "name": "endpoint_2",
                            "model_arg": "mixtral_finetune",
                            "url": "https://...",
                            "key": "custom_key_2",
                        },
                    ],
                },
            },
        },
    },
)
def list_custom_endpoints(
    request_fastapi: Request,
    custom_endpoint_dao: CustomEndpointDAO = Depends(),
) -> List[CustomEndpoint]:
    """
    Returns a list of the available custom endpoints.
    """
    user_id = request_fastapi.state.user_id
    return custom_endpoint_dao.get_user_endpoints(user_id=user_id)
