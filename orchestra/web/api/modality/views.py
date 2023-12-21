from typing import List

from fastapi import APIRouter
from fastapi.param_functions import Depends

from orchestra.db.dao.modality_dao import ModalityDAO
from orchestra.db.models.orchestra_models import Modality
from orchestra.web.api.modality.schema import (
    ModalityModelRequest,
    ModalityModelResponse,
)

router = APIRouter()


@router.get("/", response_model=List[ModalityModelResponse])
async def get_modality_models(
    limit: int = 10,
    offset: int = 0,
    modality_dao: ModalityDAO = Depends(),
) -> List[Modality]:
    """
    Retrieve all modality objects from the database.

    :param limit: limit of modality objects, defaults to 10.
    :param offset: offset of modality objects, defaults to 0.
    :param modality_dao: DAO for modality models.
    :return: list of modality objects from database.
    """
    return await modality_dao.get_all_modalities(limit=limit, offset=offset)


@router.put("/")
async def create_modality_model(
    new_modality_object: ModalityModelRequest,
    modality_dao: ModalityDAO = Depends(),
) -> None:
    """
    Creates modality model in the database.

    :param new_modality_object: new modality model item.
    :param modality_dao: DAO for modality models.
    """
    await modality_dao.create_modality(
        name=new_modality_object.name,
    )
