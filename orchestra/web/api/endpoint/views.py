import datetime
from typing import List, Optional

from fastapi import APIRouter
from fastapi.param_functions import Depends

from orchestra.db.dao.endpoint_dao import EndpointDAO
from orchestra.db.models.orchestra_models import Endpoint
from orchestra.web.api.endpoint.schema import EndpointModelResponse

router = APIRouter()


@router.get("/get_all_endpoints", response_model=List[EndpointModelResponse])
async def get_endpoint_models(
    limit: int = 10,
    offset: int = 0,
    endpoint_dao: EndpointDAO = Depends(),
) -> List[Endpoint]:
    """
    Retrieve all endpoint objects from the database.

    :param limit: limit of endpoint objects, defaults to 10.
    :param offset: offset of endpoint objects, defaults to 0.
    :param endpoint_dao: DAO for endpoint models.
    :return: list of endpoint objects from database.
    """
    return await endpoint_dao.get_all_endpoints(limit=limit, offset=offset)


@router.get("/get_endpoint", response_model=List[EndpointModelResponse])
async def get_endpoint(
    mdl_id: Optional[int] = None,
    provider_id: Optional[int] = None,
    created_at: Optional[datetime.datetime] = None,
    endpoint_dao: EndpointDAO = Depends(),
) -> List[Endpoint]:
    """
    Retrieve specific endpoint object from the database.

    :param mdl_id: mdl_id of endpoint object.
    :param provider_id: provider_id of endpoint object.
    :param created_at: created_at of endpoint object.
    :param endpoint_dao: DAO for endpoint models.
    :return: endpoint object from database.
    """
    return await endpoint_dao.filter(
        mdl_id=mdl_id,
        provider_id=provider_id,
        created_at=created_at,
    )
