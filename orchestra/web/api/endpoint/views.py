import datetime
from typing import List

from fastapi import APIRouter
from fastapi.param_functions import Depends

from orchestra.db.dao.endpoint_dao import EndpointDAO
from orchestra.db.models.orchestra_models import Endpoint
from orchestra.web.api.endpoint.schema import (
    EndpointModelRequest,
    EndpointModelResponse,
)

router = APIRouter()


@router.get("/", response_model=List[EndpointModelResponse])
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


@router.put("/")
async def create_endpoint_model(
    new_endpoint_object: EndpointModelRequest,
    endpoint_dao: EndpointDAO = Depends(),
) -> None:
    """
    Creates endpoint model in the database.

    :param new_endpoint_object: new endpoint model item.
    :param endpoint_dao: DAO for endpoint models.
    """
    created_at = datetime.datetime.now()
    await endpoint_dao.create_endpoint(
        mdl_id=new_endpoint_object.mdl_id,
        provider_id=new_endpoint_object.provider_id,
        created_at=created_at,
    )
