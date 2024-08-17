import datetime
import logging
from typing import Dict, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.param_functions import Depends

from orchestra.db.dao.recharge_dao import RechargeDAO
from orchestra.db.dao.users_dao import UsersDAO
from orchestra.db.models.orchestra_models import Users
from orchestra.web.api.credits.schema import CreditsResponse
from orchestra.web.api.utils.on_prem import handle_on_prem

router = APIRouter()

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

handler = logging.StreamHandler()
handler.setLevel(logging.INFO)
logger.addHandler(handler)


@router.get(
    "/credits",
    response_model=CreditsResponse,
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {"example": {"id": "<USER_ID>", "credits": 10}},
            },
        },
    },
)
@handle_on_prem(endpoint="/credits", method="none")
def get_credits(
    request_fastapi: Request,
    users_dao: UsersDAO = Depends(),
) -> Users:
    """
    Returns the number of available credits.
    \f
    :param request_fastapi: FastAPI request object.
    :param users_dao: DAO for users models.
    :return: user instance with credits from database.
    """
    user = users_dao.filter(id=request_fastapi.state.user_id)
    # TODO: Remove this after fixing the DB entries
    if len(user) == 0:
        logging.debug(f"##ANCHOR## bot: {request_fastapi.state.user_id}")
    return user[0]


@router.post(
    "/promo",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {"info": "Code {code} activated succesfully!"},
                },
            },
        },
        400: {
            "description": "Already activated code",
            "content": {
                "application/json": {
                    "example": {"detail": "This code has already been activated."},
                },
            },
        },
        404: {
            "description": "Code Not Found",
            "content": {"application/json": {"example": {"detail": "Invalid code."}}},
        },
    },
)
@handle_on_prem(endpoint="/promo", method="none")
def promo_code(
    request_fastapi: Request,
    code: str = Query(
        ...,
        description="Promo code to be activated.",
        example="sample_code",
    ),
    user: Optional[str] = Query(
        None,
        description=(
            "ID of the user that receives the credits,"
            "defaults to the user making the request."
        ),
        example="sample_user_id",
    ),
    recharge_dao: RechargeDAO = Depends(),
    users_dao: UsersDAO = Depends(),
) -> Dict[str, str]:
    """
    Activates a promotional code.
    \f
    :param request_fastapi: FastAPI request object.
    :param code: Promo code to be activated.
    :param user: ID of the user that receives the credits, if not present
    if defaults to the user making the request.
    :param recharge_dao: DAO for recharge models.
    :param users_dao: DAO for users models.
    :return: user instance with credits from database.
    """

    raise HTTPException(
        status_code=400,
        detail="Promo codes are not available at the moment!",
    )

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
        "PRODUCTHUNT",
    ]
    if code not in promo_codes:
        raise HTTPException(status_code=404, detail="Invalid code.")

    user_id = request_fastapi.state.user_id
    if user is not None:
        if len(users_dao.filter(id=user)) > 0:
            user_id = user
        else:
            raise HTTPException(
                status_code=404,
                detail="The specified user id doesn't exist.",
            )

    prev_recharges = recharge_dao.filter(user_id=user_id)

    if any(pr.type == code for pr in prev_recharges):
        raise HTTPException(
            status_code=400,
            detail="This code has already been activated.",
        )

    if any(pr.type in promo_codes for pr in prev_recharges):
        raise HTTPException(
            status_code=400,
            detail="You have already used a promo code!",
        )

    recharge_dao.create_recharge(
        at=datetime.datetime.now(),
        user_id=user_id,
        quantity=qty,
        type=code,
    )
    users_dao.recharge_credit(user_id, qty)
    return {"info": f"Code {code} activated succesfully!"}
