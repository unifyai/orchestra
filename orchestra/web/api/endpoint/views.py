from typing import List, Optional

from fastapi import APIRouter
from fastapi.param_functions import Depends

from orchestra.db.dao.endpoint_dao import EndpointDAO
from orchestra.db.dao.model_dao import ModelDAO
from orchestra.db.dao.provider_dao import ProviderDAO
from orchestra.web.api.endpoint.schema import EndpointModelResponseVerbose

router = APIRouter()


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
                mdl_user_id=str(model_inst.user_id),
                mdl_uploaded_at=model_inst.uploaded_at,  # type: ignore
                mdl_task=str(model_inst.task),
                mdl_description=str(model_inst.description),
                mdl_license=str(model_inst.license),
                mdl_active=model_inst.active,  # type: ignore
                mdl_input_args_format=str(model_inst.input_args_format),
                mdl_output_format=str(model_inst.output_format),
                mdl_custom_fields=str(model_inst.custom_fields),
                provider_id=int(provider_inst.id),
                provider_name=str(provider_inst.name),
                provider_image_url=str(provider_inst.image_url),
                provider_description=str(provider_inst.description),
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
                mdl_user_id=str(model_inst.user_id),
                mdl_uploaded_at=model_inst.uploaded_at,  # type: ignore
                mdl_task=str(model_inst.task),
                mdl_description=str(model_inst.description),
                mdl_license=str(model_inst.license),
                mdl_active=model_inst.active,  # type: ignore
                mdl_input_args_format=str(model_inst.input_args_format),
                mdl_output_format=str(model_inst.output_format),
                mdl_custom_fields=str(model_inst.custom_fields),
                provider_id=int(provider_inst.id),
                provider_name=str(provider_inst.name),
                provider_image_url=str(provider_inst.image_url),
                provider_description=str(provider_inst.description),
            ),
        )

    return endpoints
