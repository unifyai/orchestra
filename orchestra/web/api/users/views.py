import datetime
from typing import Optional, Union

from fastapi import APIRouter, HTTPException, Request
from fastapi.param_functions import Depends

from orchestra.db.dao.recharge_dao import RechargeDAO
from orchestra.db.dao.users_dao import UsersDAO
from orchestra.db.models.orchestra_models import Users
from orchestra.web.api.users.schema import CreditsCodeResponse, CreditsResponse

router = APIRouter()


@router.get("/get_credits", response_model=Union[CreditsResponse, None])
def get_credits(
    request_fastapi: Request,
    users_dao: UsersDAO = Depends(),
) -> Union[Users, None]:
    """
    Retrieves credits for the user doing the request.
    \f
    :param request_fastapi: FastAPI request object.
    :param users_dao: DAO for users models.
    :return: user instance with credits from database.
    """
    user = users_dao.filter(id=request_fastapi.state.user_id)
    return user[0] if user else None


@router.post("/promo", response_model=CreditsCodeResponse)
def credits_code(
    request_fastapi: Request,
    code: str,
    user: Optional[str] = None,
    recharge_dao: RechargeDAO = Depends(),
    users_dao: UsersDAO = Depends(),
) -> Union[CreditsCodeResponse, None]:
    """
    Checks if it's a valid promo code.
    \f
    :param request_fastapi: FastAPI request object.
    :param code: Promo code to be activated.
    :param recharge_dao: DAO for recharge models.
    :param users_dao: DAO for users models.
    :return: user instance with credits from database.
    """
    qty = 50
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
        "AIFurnace",
        "E2E",
        "DECODINGDATASCIENCE",
    ]
    if code not in promo_codes:
        raise HTTPException(status_code=404, detail="Invalid code.")

    user_id = request_fastapi.state.user_id
    if user is not None:
        if len(users_dao.filter(id=user)) > 0:
            user_id = user
        else:
            raise HTTPException(status_code=404, detail="The specified user id doesn't exist.")

    prev_recharges = recharge_dao.filter(user_id=user_id)

    if any(pr.type == code for pr in prev_recharges):
        raise HTTPException(status_code=400, detail="This code has already been activated.")

    if any(pr.type in promo_codes for pr in prev_recharges):
        raise HTTPException(status_code=400, detail="You have already used a promo code!")

    recharge_dao.create_recharge(
        at=datetime.datetime.now(),
        user_id=user_id,
        quantity=qty,
        type=code,
    )
    users_dao.recharge_credit(user_id, qty)
    return CreditsCodeResponse(msg=f"Code {code} activated succesfully!")
