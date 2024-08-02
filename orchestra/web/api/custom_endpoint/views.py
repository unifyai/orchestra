from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.param_functions import Depends

from orchestra.db.dao.custom_api_key_dao import CustomApiKeyDAO
from orchestra.db.dao.custom_endpoint_dao import CustomEndpointDAO
from orchestra.db.models.orchestra_models import CustomApiKey, CustomEndpoint
from orchestra.web.api.custom_endpoint.schema import (
    CustomApiKeyModelResponse,
    CustomEndpointModelResponse,
)

router = APIRouter()


@router.get("/custom_endpoint", response_model=List[CustomEndpointModelResponse])
def get_custom_endpoints(
    request_fastapi: Request,
    custom_endpoint_dao: CustomEndpointDAO = Depends(),
) -> List[CustomEndpoint]:
    """
    Returns a list of the available custom endpoints.
    """
    user_id = request_fastapi.state.user_id
    return custom_endpoint_dao.get_user_endpoints(user_id=user_id)


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


@router.put(
    "/custom_api_key",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {"info": "API key created succesfully!"},
                },
            },
        },
    },
)
def create_custom_api_key(
    request_fastapi: Request,
    key: str = Query(..., description="Name of the API key."),
    value: str = Query(..., description="Value of the API key."),
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
    return {"info": "API key created succesfully!"}


@router.put(
    "/custom_endpoint",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {"info": "Custom endpoint created succesfully!"},
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
        ...,
        description="Alias for the custom endpoint. This will be the name used to call the endpoint.",
    ),
    url: str = Query(
        ...,
        description="Base URL of the endpoint being called. Must support the OpenAI format.",
    ),
    key_name: str = Query(
        ...,
        description="Name of the API key that will be passed as part of the query.",
    ),
    mdl_name: Optional[str] = Query(
        None,
        description=(
            "Named passed to the custom endpoint as model name. "
            "If not specified, it will default to the endpoint alias."
        ),
    ),
    custom_endpoint_dao: CustomEndpointDAO = Depends(),
    custom_api_key_dao: CustomApiKeyDAO = Depends(),
) -> None:
    """
    Creates a custom endpoint. This endpoint must support the OpenAI `/chat/completions`
    format. To query your custom endpoint, replace your endpoint string with `<name>@custom`
    when querying the unified API.

    """
    user_id = request_fastapi.state.user_id
    try:
        key_id = custom_api_key_dao.filter(user_id=user_id, key=key_name)[0].id
    except Exception:
        raise HTTPException(status_code=404, detail="Custom API Key not found.")

    custom_endpoint_dao.create_custom_endpoint(
        user_id=user_id,
        name=name,
        mdl_name=mdl_name if mdl_name else name,
        url=url,
        key_id=key_id,
    )
    return {"info": "Custom endpoint created succesfully!"}
