from typing import List

from fastapi import APIRouter
from fastapi.param_functions import Depends

from orchestra.db.dao.license_dao import LicenseDAO
from orchestra.db.models.orchestra_models import License
from orchestra.web.api.license.schema import LicenseModelResponse

router = APIRouter()


@router.get("/get_all_licenses", response_model=List[LicenseModelResponse])
async def get_license_models(
    limit: int = 10,
    offset: int = 0,
    license_dao: LicenseDAO = Depends(),
) -> List[License]:
    """
    Retrieve all license objects from the database.

    :param limit: limit of license objects, defaults to 10.
    :param offset: offset of license objects, defaults to 0.
    :param license_dao: DAO for license models.
    :return: list of license objects from database.
    """
    return await license_dao.get_all_licenses(limit=limit, offset=offset)


@router.get("/get_license", response_model=List[LicenseModelResponse])
async def get_license(
    name: str,
    license_dao: LicenseDAO = Depends(),
) -> List[License]:
    """
    Retrieve specific license object from the database.

    :param name: name of license instance.
    :param license_dao: DAO for license models.
    :return: list of license objects from database.
    """
    return await license_dao.filter(name=name)
