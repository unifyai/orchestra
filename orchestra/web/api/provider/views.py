from typing import List

from fastapi import APIRouter
from fastapi.param_functions import Depends

from orchestra.db.dao.provider_dao import ProviderDAO

# Async DAOs
from orchestra.db.dao.async_provider_dao import AsyncProviderDAO
from orchestra.db.dependencies import get_async_db_session, get_db_session
from orchestra.db.models.orchestra_models import Provider
from orchestra.web.api.provider.schema import ProviderModelResponse

router = APIRouter()


@router.get("/providers", response_model=List[ProviderModelResponse])
async def get_provider_models(
    limit: int = 10,
    offset: int = 0,
    session: AsyncSession = Depends(get_async_db_session),
) -> List[Provider]:
    """
    Retrieve all provider objects from the database.
    \f
    :param limit: limit of provider objects, defaults to 10.
    :param offset: offset of provider objects, defaults to 0.
    :param provider_dao: DAO for provider models.
    :return: list of provider objects from database.
    """
    provider_dao = AsyncProviderDAO(session)
    return provider_dao.get_all_providers(limit=limit, offset=offset)


@router.get("/get_provider", response_model=List[ProviderModelResponse])
async def get_provider(
    name: str,
    session: AsyncSession = Depends(get_async_db_session),
) -> List[Provider]:
    """
    Retrieve specific provider object from the database.
    \f
    :param name: name of provider object.
    :param provider_dao: DAO for provider models.
    :return: provider object from database.
    """
    provider_dao = AsyncProviderDAO(session)
    return await provider_dao.filter(name=name)
