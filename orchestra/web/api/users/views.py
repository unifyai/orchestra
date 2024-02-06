import datetime
from typing import Union

from fastapi import APIRouter, Request
from fastapi.param_functions import Depends

from orchestra.db.dao.recharge_dao import RechargeDAO
from orchestra.db.dao.users_dao import UsersDAO
from orchestra.db.models.orchestra_models import Users
from orchestra.web.api.users.schema import CreditsCodeResponse, CreditsResponse

router = APIRouter()


@router.get("/get_credits", response_model=Union[CreditsResponse, None])
async def get_credits(
    request_fastapi: Request,
    users_dao: UsersDAO = Depends(),
) -> Union[Users, None]:
    """
    Retrieve all credits based on user id from the database.

    :param request_fastapi: FastAPI request object.
    :param users_dao: DAO for users models.
    :return: user instance with credits from database.
    """
    user = await users_dao.filter(id=request_fastapi.state.user_id)
    return user[0] if user else None


@router.post("/promo", response_model=CreditsCodeResponse)
async def credits_code(
    request_fastapi: Request,
    code: str,
    recharge_dao: RechargeDAO = Depends(),
    users_dao: UsersDAO = Depends(),
) -> Union[CreditsCodeResponse, None]:
    """
    Checks if it's a valid code and adds $2.5 credits if so.

    :param request_fastapi: FastAPI request object.
    :param code: Promo code to be activated.
    :param recharge_dao: DAO for recharge models.
    :param users_dao: DAO for users models.
    :return: user instance with credits from database.
    """
    promo_codes = [
        "HACKERNEWS",
        "DEEPDIVE",
        "PORT",
        "DISCORD",
        "YCW23",
        "YCS23",
        "ESSENCE",
        "LIFTOFF",
        "PHOENIX",
        "LVLUP",
        "YCOSS",
        "PIONEER",
        "HYPERION",
        "YCW23-SOLO",
        "IGNITE",
        "LINKED",
        "CONTRIB",
        "ALUMNI",
        "LAUNCHBF",
    ]
    if code not in promo_codes:
        return CreditsCodeResponse(msg="Invalid code.")

    user_id = request_fastapi.state.user_id

    prev_recharges = await recharge_dao.filter(user_id=user_id)

    if any(pr.type == code for pr in prev_recharges):
        return CreditsCodeResponse(msg="This code is already activated!")
    
    if any(pr.type in promo_codes for pr in prev_recharges):
        return CreditsCodeResponse(msg="You have already used a promo code!")

    # recharge 2.5
    await recharge_dao.create_recharge(
        at=datetime.datetime.now(),
        user_id=user_id,
        quantity=2.5,
        type=code,
    )
    await users_dao.recharge_credit(user_id, 2.5)
    return CreditsCodeResponse(msg=f"Code {code} activated succesfully!")
