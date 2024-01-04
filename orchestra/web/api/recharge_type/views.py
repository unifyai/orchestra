from typing import List

from fastapi import APIRouter
from fastapi.param_functions import Depends

from orchestra.db.dao.recharge_type_dao import RechargeTypeDAO
from orchestra.db.models.orchestra_models import RechargeType
from orchestra.web.api.recharge_type.schema import RechargeTypeModelResponse

router = APIRouter()


@router.get("/get_all_recharge_types", response_model=List[RechargeTypeModelResponse])
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


@router.get("/get_recharge_type", response_model=List[RechargeTypeModelResponse])
async def get_recharge_type(
    type: str,  # noqa: WPS125
    recharge_type_dao: RechargeTypeDAO = Depends(),
) -> List[RechargeType]:
    """
    Retrieve specific recharge_type object from the database.

    :param type: type of recharge_type object.
    :param recharge_type_dao: DAO for recharge_type models.
    :return: recharge_type object from database.
    """
    return await recharge_type_dao.filter(type=type)
