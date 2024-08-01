import time
from typing import List, Optional

from fastapi import APIRouter, Query
from fastapi.param_functions import Depends

from orchestra.db.dao.endpoint_dao import EndpointDAO
from orchestra.db.dao.model_dao import ModelDAO
from orchestra.db.dao.provider_dao import ProviderDAO
from orchestra.web.api.endpoint.schema import EndpointModelResponseVerbose
from orchestra.web.api.utils.http_responses import overspecified_model_provider

router = APIRouter()
public_router = APIRouter()

_endpoint_list_cache = {}


@public_router.get(
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
def get_endpoints_of(
    model: str = Query(
        default=None,
        description="Model to get available endpoints from.",
    ),
    provider: str = Query(
        default=None,
        description="Provider to get available endpoints from.",
    ),
    endpoint_dao: EndpointDAO = Depends(),
):
    """
    Lists available endpoints. You can pass either: a model string, a provider string, or neither to get all supported endpoints.
    """
    if model and provider:
        raise overspecified_model_provider
    if (
        model,
        provider,
    ) not in _endpoint_list_cache or time.time() - _endpoint_list_cache[
        (model, provider)
    ].get(
        "ts",
        0,
    ) > 3600:
        _endpoint_list_cache[(model, provider)] = {}
        _endpoint_list_cache[(model, provider)]["ts"] = time.time()
        res = endpoint_dao.get_endpoints_of((model,), (provider,))
        if model:
            _endpoint_list_cache[(model, provider)]["strings"] = sorted(
                [f"{r.Provider.name}" for r in res],
            )
        elif provider:
            _endpoint_list_cache[(model, provider)]["strings"] = sorted(
                [f"{r.Model.mdl_code}" for r in res],
            )
        else:
            _endpoint_list_cache[(model, provider)]["strings"] = sorted(
                [f"{r.Model.mdl_code}@{r.Provider.name}" for r in res],
            )

    return _endpoint_list_cache[(model, provider)]["strings"]


_provider_list_cache = {}


@public_router.get(
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
def get_providers_of(
    model: str = Query(
        default=None,
        description="Model to get available providers from. If empty, will return all providers",
    ),
    endpoint_dao: EndpointDAO = Depends(),
):
    """
    Lists available providers for a given model.
    """
    if (
        model not in _provider_list_cache
        or time.time() - _provider_list_cache[model].get("ts", 0) > 3600
    ):
        _provider_list_cache[model] = {}
        _provider_list_cache[model]["ts"] = time.time()
        res = endpoint_dao.get_endpoints_of((model,))
        ret = list(set([r.Provider.name for r in res]))
        ret.sort()
        _provider_list_cache[model]["strings"] = ret
    return _provider_list_cache[model]["strings"]


@router.get("/endpoints", response_model=List[EndpointModelResponseVerbose])
def get_endpoint_models(  # noqa: WPS210
    limit: int = 10,
    offset: int = 0,
    endpoint_dao: EndpointDAO = Depends(),
    model_dao: ModelDAO = Depends(),
    provider_dao: ProviderDAO = Depends(),
) -> List[EndpointModelResponseVerbose]:
    """
    Retrieve all endpoint objects from the database.
    \f
    :param limit: limit of endpoint objects, defaults to 10.
    :param offset: offset of endpoint objects, defaults to 0.
    :param endpoint_dao: DAO for endpoint models.
    :param model_dao: DAO for model models.
    :param provider_dao: DAO for provider models.
    :return: list of endpoint objects from database.
    """
    raw_endpoints = endpoint_dao.get_all_endpoints_raw(limit=limit, offset=offset)

    endpoints = []
    for raw_endpoint in raw_endpoints:
        model = model_dao.filter(id=int(raw_endpoint.mdl_id))
        provider = provider_dao.filter(id=int(raw_endpoint.provider_id))

        model_inst = model[0]
        provider_inst = provider[0]
        endpoints.append(
            EndpointModelResponseVerbose(
                endpoint_id=int(raw_endpoint.id),
                created_at=raw_endpoint.created_at,  # type: ignore
                mdl_id=int(model_inst.id),
                mdl_code=str(model_inst.mdl_code),
                mdl_uploaded_at=model_inst.uploaded_at,  # type: ignore
                mdl_task=str(model_inst.task),
                mdl_active=model_inst.active,  # type: ignore
                provider_id=int(provider_inst.id),
                provider_name=str(provider_inst.name),
                provider_image_url=str(provider_inst.image_url),
            ),
        )

    return endpoints


@router.get("/get_endpoint", response_model=List[EndpointModelResponseVerbose])
def get_endpoint(  # noqa: WPS210, WPS211, WPS217
    endpoint_id: Optional[int] = None,
    mdl_id: Optional[int] = None,
    provider_id: Optional[int] = None,
    endpoint_dao: EndpointDAO = Depends(),
    model_dao: ModelDAO = Depends(),
    provider_dao: ProviderDAO = Depends(),
) -> List[EndpointModelResponseVerbose]:
    """
    Retrieve specific endpoint object from the database.
    \f
    :param endpoint_id: endpoint_id of endpoint instance.
    :param mdl_id: mdl_id of endpoint instance.
    :param provider_id: provider_id of endpoint instance.
    :param endpoint_dao: DAO for endpoint models.
    :param model_dao: DAO for model models.
    :param provider_dao: DAO for provider models.
    :return: list of endpoint objects from database.
    """
    if endpoint_id:
        raw_endpoints = endpoint_dao.filter(id=endpoint_id)
    elif mdl_id:
        raw_endpoints = endpoint_dao.filter(mdl_id=mdl_id)
    elif provider_id:
        raw_endpoints = endpoint_dao.filter(provider_id=provider_id)
    else:
        raw_endpoints = endpoint_dao.get_all_endpoints_raw(limit=10, offset=0)

    endpoints = []
    for raw_endpoint in raw_endpoints:
        model = model_dao.filter(id=int(raw_endpoint.mdl_id))
        provider = provider_dao.filter(id=int(raw_endpoint.provider_id))

        model_inst = model[0]
        provider_inst = provider[0]
        endpoints.append(
            EndpointModelResponseVerbose(
                endpoint_id=int(raw_endpoint.id),
                created_at=raw_endpoint.created_at,  # type: ignore
                mdl_id=int(model_inst.id),
                mdl_code=str(model_inst.mdl_code),
                mdl_uploaded_at=model_inst.uploaded_at,  # type: ignore
                mdl_task=str(model_inst.task),
                mdl_active=model_inst.active,  # type: ignore
                provider_id=int(provider_inst.id),
                provider_name=str(provider_inst.name),
                provider_image_url=str(provider_inst.image_url),
            ),
        )

    return endpoints
