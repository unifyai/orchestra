from typing import Dict, List

from fastapi import APIRouter, Query, Request
from fastapi.param_functions import Depends

# Async DAOs
from sqlalchemy.ext.asyncio import AsyncSession

from orchestra.db.dependencies import get_async_db_session
from orchestra.web.api.custom_api_keys.schema import CustomApiKeyModelResponse
from orchestra.web.api.utils.http_responses import not_found

router = APIRouter()


@router.post(
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
async def create_custom_api_key(
    request_fastapi: Request,
    name: str = Query(description="Name of the API key.", example="key1"),
    value: str = Query(description="Value of the API key.", example="value1"),
    session: AsyncSession = Depends(get_async_db_session),
) -> Dict[str, str]:
    """
    Stores a custom API key from an LLM provider in your account. This can be done in
    one of two ways:

    1. As part of a custom endpoint. If you define a custom endpoint, you can reference
    a custom API key. This will be sent to the endpoint as part of the request.

    2. To use your own API keys with the standard providers. If any of your custom API
    keys match a provider name and you pass `use_custom_keys=True` to the
    `/chat/completions` endpoint, then this API key will be used, using your own
    account with the provider directly.

    """
    user_id = request_fastapi.state.user_id
    custom_api_key_dao = AsyncCustomAsyncApiKeyDAO(session)
    custom_api_key_dao.create_custom_api_key(
        user_id=user_id,
        key=name,
        value=value,
    )
    return {"info": "API key created successfully!"}


@router.get(
    "/custom_api_key",
    response_model=CustomApiKeyModelResponse,
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {
                        "name": "custom_api_key_name",
                        "value": "custom_api_key_value",
                    },
                },
            },
        },
        404: {
            "description": "Custom API key Not Found",
            "content": {
                "application/json": {
                    "example": {"detail": "Custom API key not found."},
                },
            },
        },
    },
)
async def get_custom_api_key(
    request_fastapi: Request,
    name: str = Query(
        description="Name of the API key to get the value for.",
        example="key1",
    ),
    session: AsyncSession = Depends(get_async_db_session),
) -> CustomApiKeyModelResponse:
    """
    Returns the value of the key for the specified custom API key name.
    """
    user_id = request_fastapi.state.user_id
    custom_api_key_dao = AsyncCustomAsyncApiKeyDAO(session)
    all_keys = custom_api_key_dao.get_user_keys(user_id=user_id)
    for api_key in all_keys:
        if api_key.key == name:
            return CustomApiKeyModelResponse(name=api_key.key, value=api_key.value)
    raise not_found("Custom API Key")


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
                    "example": {"detail": "Custom API key not found."},
                },
            },
        },
    },
)
async def delete_custom_api_key(
    request_fastapi: Request,
    name: str = Query(
        description="Name of the custom API key to delete.",
        example="key1",
    ),
    session: AsyncSession = Depends(get_async_db_session),
) -> Dict[str, str]:
    """
    Deletes the custom API key from your account.

    """
    user_id = request_fastapi.state.user_id
    custom_api_key_dao = AsyncCustomAsyncApiKeyDAO(session)
    existing_key = await custom_api_key_dao.filter(user_id=user_id, key=name)
    if not existing_key:
        raise not_found("Custom API Key")

    await custom_api_key_dao.delete(
        user_id=user_id,
        name=name,
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
                    "example": {"detail": "Custom API key not found."},
                },
            },
        },
    },
)
async def rename_custom_api_key(
    request_fastapi: Request,
    name: str = Query(
        description="Name of the custom API key to be updated.",
        example="key1",
    ),
    new_name: str = Query(
        description="New name for the custom API key.",
        example="key2",
    ),
    session: AsyncSession = Depends(get_async_db_session),
) -> Dict[str, str]:
    """
    Renames the custom API key in your account.

    """
    user_id = request_fastapi.state.user_id
    custom_api_key_dao = AsyncCustomAsyncApiKeyDAO(session)
    existing_key = await custom_api_key_dao.filter(user_id=user_id, key=name)
    if not existing_key:
        raise not_found("Custom API Key")

    custom_api_key_dao.rename(
        user_id=user_id,
        name=name,
        new_name=new_name,
    )
    return {"info": "API key renamed successfully!"}


@router.get(
    "/custom_api_key/list",
    response_model=List[CustomApiKeyModelResponse],
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": [
                        {"name": "custom_key_1", "value": "****alue_1"},
                        {"name": "custom_key_2", "value": "****alue_2"},
                    ],
                },
            },
        },
    },
)
async def list_custom_api_keys(
    request_fastapi: Request,
    session: AsyncSession = Depends(get_async_db_session),
) -> List[CustomApiKeyModelResponse]:
    """
    Returns a list of the names for all custom API keys in your account.
    """
    user_id = request_fastapi.state.user_id
    custom_api_key_dao = AsyncCustomAsyncApiKeyDAO(session)
    raw_response = custom_api_key_dao.get_user_keys(user_id=user_id)
    return [
        CustomApiKeyModelResponse(name=rr.key, value=rr.value) for rr in raw_response
    ]
