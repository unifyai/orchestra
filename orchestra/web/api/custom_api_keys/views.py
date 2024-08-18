from typing import List
from fastapi.param_functions import Depends
from fastapi import APIRouter, Query, Request

from orchestra.db.models.orchestra_models import CustomApiKey
from orchestra.db.dao.custom_api_key_dao import CustomApiKeyDAO
from orchestra.web.api.custom_api_keys.schema import CustomApiKeyModelResponse
from orchestra.web.api.utils.http_responses import custom_api_key_not_found

router = APIRouter()


@router.put(
    "/custom_api_key",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {"info": "API key created successfully!"},
                },
            },
        },
    },
)
def create_custom_api_key(
    request_fastapi: Request,
    key: str = Query(description="Name of the API key.", example="key1"),
    value: str = Query(description="Value of the API key.", example="value1"),
    custom_api_key_dao: CustomApiKeyDAO = Depends(),
) -> None:
    """
    Stores a custom API key from a LLM provider in your account. This can be used in two ways:
    1. As part of a custom endpoint. If you define a custom endpoint, you can reference a custom API
    key. This will be sent to the endpoint as part of the request.
    2. To use your own API keys in standard providers. If any of your custom API keys matches a provider
    name and you pass `use_custom_keys=True` to the `/chat/completions` endpoint, this API key will
    be used, charging your account directly instead of consuming Unify credits.

    """
    user_id = request_fastapi.state.user_id
    custom_api_key_dao.create_custom_api_key(
        user_id=user_id,
        key=key,
        value=value,
    )
    return {"info": "API key created successfully!"}


@router.get("/custom_api_key", response_model=List[CustomApiKeyModelResponse])
def get_custom_api_keys(
    request_fastapi: Request,
    custom_api_key_dao: CustomApiKeyDAO = Depends(),
) -> List[CustomApiKey]:
    """
    Returns a list of the available custom API keys.
    """
    user_id = request_fastapi.state.user_id
    return custom_api_key_dao.get_user_keys(user_id=user_id)


@router.delete(
    "/custom_api_key",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {"info": "API key deleted successfully!"},
                },
            },
        },
        404: {
            "description": "Custom API key Not Found",
            "content": {
                "application/json": {
                    "example": {"detail": "API key not found."},
                },
            },
        },
    },
)
def delete_custom_api_key(
    request_fastapi: Request,
    key: str = Query(
        description="Name of the custom API key to delete.",
        example="key1",
    ),
    custom_api_key_dao: CustomApiKeyDAO = Depends(),
) -> None:
    """
    Deletes a custom API key in your account.

    """
    user_id = request_fastapi.state.user_id

    existing_key = custom_api_key_dao.filter(user_id=user_id, key=key)
    if not existing_key:
        raise custom_api_key_not_found

    custom_api_key_dao.delete(
        user_id=user_id,
        name=key,
    )
    return {"info": "API key deleted successfully!"}


@router.post(
    "/custom_api_key/rename",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {"info": "API key renamed successfully!"},
                },
            },
        },
        404: {
            "description": "Custom API key Not Found",
            "content": {
                "application/json": {
                    "example": {"detail": "API key not found."},
                },
            },
        },
    },
)
def rename_custom_api_key(
    request_fastapi: Request,
    key: str = Query(
        description="Name of the custom API key to be updated.",
        example="key1",
    ),
    new_key: str = Query(
        description="New name for the custom API key.",
        example="key2",
    ),
    custom_api_key_dao: CustomApiKeyDAO = Depends(),
) -> None:
    """
    Renames a custom API key from a LLM provider in your account.

    """
    user_id = request_fastapi.state.user_id

    existing_key = custom_api_key_dao.filter(user_id=user_id, key=key)
    if not existing_key:
        raise custom_api_key_not_found

    custom_api_key_dao.rename(
        user_id=user_id,
        name=key,
        new_name=new_key,
    )
    return {"info": "API key renamed successfully!"}


ALLOWED_METRICS = [
    "input-cost",
    "output-cost",
    "tokens-per-second",
    "time-to-first-token",
    "inter-token-latency",
    "end-2-end-latency",
    "cold-start",
]
ALLOWED_METRICS_STR = ""
for metric in ALLOWED_METRICS:
    ALLOWED_METRICS_STR += f'"{metric}", '
ALLOWED_METRICS_STR = ALLOWED_METRICS_STR[:-2]
