from typing import List

from fastapi import APIRouter
from fastapi.param_functions import Depends

from orchestra.db.dao.recharge_type_dao import RechargeTypeDAO
from orchestra.db.models.orchestra_models import RechargeType
from orchestra.web.api.recharge_type.schema import (
    RechargeTypeModelRequest,
    RechargeTypeModelResponse,
)

router = APIRouter()


@router.get("/", response_model=List[RechargeTypeModelResponse])
async def get_recharge_type_models(
    limit: int = 10,
    offset: int = 0,
    recharge_type_dao: RechargeTypeDAO = Depends(),
) -> List[RechargeType]:
    """
    Retrieve all recharge_type objects from the database.

    :param limit: limit of recharge_type objects, defaults to 10.
    :param offset: offset of recharge_type objects, defaults to 0.
    :param recharge_type_dao: DAO for recharge_type models.
    :return: list of recharge_type objects from database.
    """
    return await recharge_type_dao.get_all_recharge_types(limit=limit, offset=offset)


@router.put("/")
async def create_recharge_type_model(
    new_recharge_type_object: RechargeTypeModelRequest,
    recharge_type_dao: RechargeTypeDAO = Depends(),
) -> None:
    """
    Creates recharge_type model in the database.

    :param new_recharge_type_object: new recharge_type model item.
    :param recharge_type_dao: DAO for recharge_type models.
    """
    await recharge_type_dao.create_recharge_type(
        type=new_recharge_type_object.type,
    )
