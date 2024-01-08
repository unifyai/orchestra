import datetime
from typing import List

from fastapi import APIRouter
from fastapi.param_functions import Depends

from orchestra.db.dao.recharge_dao import RechargeDAO
from orchestra.db.models.orchestra_models import Recharge
from orchestra.web.api.recharge.schema import RechargeModelResponse

router = APIRouter()


@router.get("/get_all_recharges", response_model=List[RechargeModelResponse])
async def get_recharge_models(
    limit: int = 10,
    offset: int = 0,
    recharge_dao: RechargeDAO = Depends(),
) -> List[Recharge]:
    """
    Retrieve all recharge objects from the database.

    :param limit: limit of recharge objects, defaults to 10.
    :param offset: offset of recharge objects, defaults to 0.
    :param recharge_dao: DAO for recharge models.
    :return: list of recharge objects from database.
    """
    return await recharge_dao.get_all_recharges(limit=limit, offset=offset)


@router.get("/get_recharge", response_model=List[RechargeModelResponse])
async def get_recharge(
    at: datetime.datetime = datetime.datetime.now(),
    user_id: str = "",
    quantity: float = 0,
    type: str = "",  # noqa: WPS125
    recharge_dao: RechargeDAO = Depends(),
) -> List[Recharge]:
    """
    Retrieve specific recharge object from the database.

    :param at: at of recharge instance.
    :param user_id: user_id of recharge instance.
    :param quantity: quantity of recharge instance.
    :param type: type of recharge instance.
    :param recharge_dao: DAO for recharge models.
    :return: list of recharge objects from database.
    """
    return await recharge_dao.filter(
        at=at,
        user_id=user_id,
        quantity=quantity,
        type=type,
    )
