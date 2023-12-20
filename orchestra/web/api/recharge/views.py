import datetime
from typing import List

from fastapi import APIRouter
from fastapi.param_functions import Depends

from orchestra.db.dao.recharge_dao import RechargeDAO
from orchestra.db.models.orchestra_models import Recharge
from orchestra.web.api.recharge.schema import (
    RechargeModelRequest,
    RechargeModelResponse,
)

router = APIRouter()


@router.get("/", response_model=List[RechargeModelResponse])
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


@router.put("/")
async def create_recharge_model(
    new_recharge_object: RechargeModelRequest,
    recharge_dao: RechargeDAO = Depends(),
) -> None:
    """
    Creates recharge model in the database.

    :param new_recharge_object: new recharge model item.
    :param recharge_dao: DAO for recharge models.
    """
    at = datetime.datetime.now()
    await recharge_dao.create_recharge(
        at=at,
        user_id=new_recharge_object.user_id,
        quantity=new_recharge_object.quantity,
        type=new_recharge_object.type,
    )
