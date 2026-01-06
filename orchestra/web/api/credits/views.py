import logging
from datetime import date
from decimal import Decimal
from typing import Dict

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.param_functions import Depends

from orchestra.db.dao.async_users_dao import AsyncUsersDAO
from orchestra.db.dao.recharge_dao import RechargeDAO
from orchestra.db.dao.users_dao import UsersDAO
from orchestra.db.dependencies import get_async_db_session, get_db_session
from orchestra.db.models.orchestra_models import Users
from orchestra.lib.time import month_end_utc
from orchestra.web.api.credits.schema import (
    CreditsResponse,
    DeductCreditsRequest,
    DeductCreditsResponse,
)
from orchestra.web.api.utils.http_responses import not_found

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
async def get_credits(
    request_fastapi: Request,
    session=Depends(get_async_db_session),
) -> Users:
    """
    Returns the number of available credits.
    \f
    :param request_fastapi: FastAPI request object.
    :param session: Async database session.
    :return: user instance with credits from database.
    """
    users_dao = AsyncUsersDAO(session)
    user = await users_dao.filter(id=request_fastapi.state.user_id)
    # TODO: Remove this after fixing the DB entries
    if len(user) == 0:
        logging.debug(f"##ANCHOR## bot: {request_fastapi.state.user_id}")
    return user[0]


@router.post(
    "/credits/deduct",
    response_model=DeductCreditsResponse,
    responses={
        200: {
            "description": "Credits deducted successfully",
            "content": {
                "application/json": {
                    "example": {
                        "previous_credits": 10.0,
                        "deducted": 2.5,
                        "current_credits": 7.5,
                    },
                },
            },
        },
        400: {
            "description": "Invalid request or insufficient credits",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Insufficient credits. Available: 5.0, requested: 10.0",
                    },
                },
            },
        },
    },
)
def deduct_credits(
    request_fastapi: Request,
    request: DeductCreditsRequest,
    session=Depends(get_db_session),
) -> DeductCreditsResponse:
    """
    Deducts credits from the user's account.

    The amount must be positive and cannot exceed the user's available credits.
    This endpoint can only deduct credits, not add them.
    \f
    :param request_fastapi: FastAPI request object.
    :param request: Request body containing the amount to deduct.
    :param session: Database session.
    :return: Response with previous, deducted, and current credit amounts.
    """
    users_dao = UsersDAO(session)
    user = users_dao.filter(id=request_fastapi.state.user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    current_credits = float(user[0].credits)

    if request.amount > current_credits:
        raise HTTPException(
            status_code=400,
            detail=f"Insufficient credits. Available: {current_credits}, requested: {request.amount}",
        )

    users_dao.recharge_credit(request_fastapi.state.user_id, -request.amount)
    session.commit()

    new_credits = current_credits - request.amount

    return DeductCreditsResponse(
        previous_credits=current_credits,
        deducted=request.amount,
        current_credits=new_credits,
    )


@router.post(
    "/promo",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {"info": "Code {code} activated successfully!"},
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
def promo_code(
    request_fastapi: Request,
    code: str = Query(
        description="Promo code to be activated.",
        example="sample_code",
    ),
    user: str = Query(
        None,
        description=(
            "ID of the user that receives the credits,"
            "defaults to the user making the request."
        ),
        example="sample_user_id",
    ),
    session=Depends(get_db_session),
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
    recharge_dao = RechargeDAO(session)
    users_dao = UsersDAO(session)

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
        raise not_found("Code")

    user_id = request_fastapi.state.user_id
    if user is not None:
        if len(users_dao.filter(id=user)) > 0:
            user_id = user
        else:
            raise not_found("User ID")

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
        user_id=user_id,
        quantity=qty,
        amount_usd=Decimal("0.00"),
        invoice_group=month_end_utc(date.today()),
        type_=code,
    )
    users_dao.recharge_credit(user_id, qty)
    return {"info": f"Code {code} activated successfully!"}
