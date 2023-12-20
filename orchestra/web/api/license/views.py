from typing import List

from fastapi import APIRouter
from fastapi.param_functions import Depends

from orchestra.db.dao.license_dao import LicenseDAO
from orchestra.db.models.orchestra_models import License
from orchestra.web.api.license.schema import LicenseModelRequest, LicenseModelResponse

router = APIRouter()


@router.get("/", response_model=List[LicenseModelResponse])
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


@router.put("/")
async def create_license_model(
    new_license_object: LicenseModelRequest,
    license_dao: LicenseDAO = Depends(),
) -> None:
    """
    Creates license model in the database.

    :param new_license_object: new license model item.
    :param license_dao: DAO for license models.
    """
    await license_dao.create_license(
        name=new_license_object.name,
        image_url=new_license_object.image_url,
        description=new_license_object.description,
    )
