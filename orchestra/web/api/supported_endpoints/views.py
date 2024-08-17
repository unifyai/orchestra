from typing import List

from fastapi import APIRouter, Query, Request
from fastapi.param_functions import Depends

from orchestra.db.dao.custom_endpoint_dao import CustomEndpointDAO
from orchestra.db.dao.endpoint_dao import EndpointDAO
from orchestra.db.models.orchestra_models import Model
from orchestra.web.api.utils.http_responses import overspecified_model_provider
from orchestra.web.api.utils.on_prem import handle_on_prem

router = APIRouter()


@router.get(
    "/providers",
    response_model=List[str],
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": ["provider_1", "provider_2", "..."],
                },
            },
        },
    },
)
@handle_on_prem(endpoint="/providers", method="get")
def get_providers(
    request_fastapi: Request,
    model: str = Query(
        default=None,
        description="Model to get available providers from.",
        example="llama-3.1-405b-chat",
    ),
    endpoint_dao: EndpointDAO = Depends(),
    custom_endpoint_dao: CustomEndpointDAO = Depends(),
):
    """
    Lists available providers. If `model` is specified,
    returns the providers that support that model.
    """
    user_id = request_fastapi.state.user_id

    res = endpoint_dao.get_endpoints_of((model,))
    providers = list(set([r.Provider.name for r in res]))

    private_endpoints_raw = custom_endpoint_dao.get_user_endpoints(user_id=user_id)
    private_models = [e[0] for e in private_endpoints_raw]

    if model and model in private_models:
        providers.append("custom")

    if not model and len(private_endpoints_raw) > 0:
        providers.append("custom")

    return sorted(providers)


@router.get(
    "/models",
    response_model=List[str],
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": ["model_a", "model_b", "..."],
                },
            },
        },
    },
)
@handle_on_prem(endpoint="/models", method="get")
def get_models(
    fastapi_request: Request,
    provider: str = Query(
        default=None,
        description="Provider to get available models from.",
        example="openai",
    ),
    endpoint_dao: EndpointDAO = Depends(),
    custom_endpoint_dao: CustomEndpointDAO = Depends(),
) -> List[Model]:
    """
    Lists available models. If `provider` is specified,
    returns the models that the provider supports.
    """
    user_id = fastapi_request.state.user_id

    raw = endpoint_dao.get_endpoints_of((None,), (provider,))
    models = list(set([r.Model.mdl_code for r in raw]))

    private_endpoints_raw = custom_endpoint_dao.get_user_endpoints(user_id=user_id)
    private_models = [e[0] for e in private_endpoints_raw]

    if provider and provider != "custom":
        private_models = []

    return sorted(models + private_models)


@router.get(
    "/endpoints",
    response_model=List[str],
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": ["model_a@provider_1", "model_a@provider_2", "..."],
                },
            },
        },
    },
)
@handle_on_prem(endpoint="/endpoints", method="get")
def get_endpoints(
    request_fastapi: Request,
    model: str = Query(
        default=None,
        description="Model to get available endpoints from.",
        example="llama-3.1-405b-chat",
    ),
    provider: str = Query(
        default=None,
        description="Provider to get available endpoints from.",
        example="openai",
    ),
    endpoint_dao: EndpointDAO = Depends(),
    custom_endpoint_dao: CustomEndpointDAO = Depends(),
):
    """
    Lists available endpoints in `model@provider` format.
    If `model` or `provider` are specified, only the matching endpoints will be listed.
    """
    user_id = request_fastapi.state.user_id
    if model and provider:
        raise overspecified_model_provider

    res = endpoint_dao.get_endpoints_of((model,), (provider,))
    endpoints = list(set([f"{r.Model.mdl_code}@{r.Provider.name}" for r in res]))

    private_endpoints_raw = custom_endpoint_dao.get_user_endpoints(user_id=user_id)
    private_endpoints = [f"{e[0]}@custom" for e in private_endpoints_raw]

    if model and f"{model}@custom" in private_endpoints:
        endpoints.append(f"{model}@custom")

    if provider and provider == "custom":
        endpoints += private_endpoints

    if not model and not provider:
        endpoints += private_endpoints

    return sorted(endpoints)
