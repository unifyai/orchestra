from typing import List

from fastapi import APIRouter, Query, Request
from fastapi.param_functions import Depends

from orchestra.db.dao.endpoint_dao import EndpointDAO
from orchestra.db.dependencies import get_db_session
from orchestra.web.api.utils.http_responses import overspecified_model_provider

router = APIRouter()


@router.get(
    "/providers",
    response_model=List[str],
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": ["openai", "anthropic", "together-ai", "..."],
                },
            },
        },
    },
)
def list_providers(
    request_fastapi: Request,
    model: str = Query(
        default=None,
        description="Model to get available providers for.",
        example="llama-3.1-405b-chat",
    ),
    session=Depends(get_db_session),
):
    """
    Lists available providers. If `model` is specified,
    returns the providers that support that model.
    """
    endpoint_dao = EndpointDAO(session)

    res = endpoint_dao.get_endpoints_of((model,))
    providers = list(set([r.Provider.name for r in res]))

    return sorted(providers)


@router.get(
    "/models",
    response_model=List[str],
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": ["gpt-3.5-turbo", "gpt-4", "claude-3-haiku", "..."],
                },
            },
        },
    },
)
def list_models(
    request_fastapi: Request,
    provider: str = Query(
        default=None,
        description="Provider to get available models from.",
        example="openai",
    ),
    session=Depends(get_db_session),
):
    """
    Lists available models. If `provider` is specified,
    returns the models that the provider supports.
    """
    endpoint_dao = EndpointDAO(session)

    raw = endpoint_dao.get_endpoints_of((None,), (provider,))
    models = list(set([r.Model.mdl_code for r in raw]))

    return sorted(models)


@router.get(
    "/endpoints",
    response_model=List[str],
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": [
                        "claude-3-haiku@anthropic",
                        "llama-3-70b-chat@groq",
                        "mistral-large@mistral-ai",
                        "...",
                    ],
                },
            },
        },
    },
)
def list_endpoints(
    request_fastapi: Request,
    model: str = Query(
        default=None,
        description="Model to get available endpoints for.",
        example="llama-3.1-405b-chat",
    ),
    provider: str = Query(
        default=None,
        description="Provider to get available endpoints for.",
        example="openai",
    ),
    session=Depends(get_db_session),
):
    """
    Lists available endpoints in `model@provider` format.
    If `model` or `provider` are specified, only the matching endpoints will be listed.
    """
    endpoint_dao = EndpointDAO(session)

    if model and provider:
        raise overspecified_model_provider

    res = endpoint_dao.get_endpoints_of((model,), (provider,))
    endpoints = list(set([f"{r.Model.mdl_code}@{r.Provider.name}" for r in res]))

    return sorted(endpoints)
