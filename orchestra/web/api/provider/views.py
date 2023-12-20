from typing import List

from fastapi import APIRouter
from fastapi.param_functions import Depends

from orchestra.db.dao.provider_dao import ProviderDAO
from orchestra.db.models.orchestra_models import Provider
from orchestra.web.api.provider.schema import (
    ProviderModelRequest,
    ProviderModelResponse,
)

router = APIRouter()


@router.get("/", response_model=List[ProviderModelResponse])
async def get_provider_models(
    limit: int = 10,
    offset: int = 0,
    provider_dao: ProviderDAO = Depends(),
) -> List[Provider]:
    """
    Retrieve all provider objects from the database.

    :param limit: limit of provider objects, defaults to 10.
    :param offset: offset of provider objects, defaults to 0.
    :param provider_dao: DAO for provider models.
    :return: list of provider objects from database.
    """
    return await provider_dao.get_all_providers(limit=limit, offset=offset)


@router.put("/")
async def create_provider_model(
    new_provider_object: ProviderModelRequest,
    provider_dao: ProviderDAO = Depends(),
) -> None:
    """
    Creates provider model in the database.

    :param new_provider_object: new provider model item.
    :param provider_dao: DAO for provider models.
    """
    await provider_dao.create_provider(
        name=new_provider_object.name,
        image_url=new_provider_object.image_url,
        description=new_provider_object.description,
    )
