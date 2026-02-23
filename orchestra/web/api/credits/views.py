import logging
from datetime import date
from decimal import Decimal
from typing import Dict

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.param_functions import Depends

from orchestra.db.dao.recharge_dao import RechargeDAO
from orchestra.db.dao.user_dao import UserDAO
from orchestra.db.dependencies import get_db_session
from orchestra.lib.billing import get_billing_entity
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
def get_credits(
    request_fastapi: Request,
    session=Depends(get_db_session),
) -> dict:
    """
    Returns the number of available credits.
    \f
    :param request_fastapi: FastAPI request object.
    :param session: Database session.
    :return: dict with user id and credits from billing account.
    """
    user_id = request_fastapi.state.user_id
    organization_id = getattr(request_fastapi.state, "organization_id", None)

    try:
        billing_entity = get_billing_entity(session, user_id, organization_id)
        credits = float(billing_entity.credits)
    except ValueError:
        credits = 0.0

    # Return the entity whose credits were looked up
    entity_id = str(organization_id) if organization_id else user_id
    return {"id": entity_id, "credits": credits}


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
    from orchestra.lib.billing import deduct_credits as billing_deduct_credits

    user_id = request_fastapi.state.user_id
    organization_id = getattr(request_fastapi.state, "organization_id", None)

    try:
        billing_entity = get_billing_entity(session, user_id, organization_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Billing is not set up")

    current_credits = float(billing_entity.credits)

    if request.amount > current_credits:
        raise HTTPException(
            status_code=400,
            detail=f"Insufficient credits. Available: {current_credits}, requested: {request.amount}",
        )

    billing_deduct_credits(session, billing_entity, Decimal(str(request.amount)))
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
    :param user_dao: DAO for users models.
    :return: user instance with credits from database.
    """
    recharge_dao = RechargeDAO(session)
    user_dao = UserDAO(session)

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
        if len(user_dao.filter(id=user)) > 0:
            user_id = user
        else:
            raise not_found("User ID")

    # Resolve billing_account_id from user
    target_user = user_dao.filter(id=user_id)
    if not target_user or not target_user[0][0].billing_account_id:
        raise HTTPException(
            status_code=400,
            detail="User not found or has no billing account",
        )
    ba_id = target_user[0][0].billing_account_id

    prev_recharges = recharge_dao.filter(billing_account_id=ba_id)

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
        billing_account_id=ba_id,
        quantity=qty,
        amount_usd=Decimal("0.00"),
        invoice_group=month_end_utc(date.today()),
        type_=code,
    )
    # Credit the billing account directly
    from orchestra.db.dao.billing_account_dao import BillingAccountDAO

    billing_account_dao = BillingAccountDAO(session)
    billing_account_dao.add_credits(ba_id, qty)
    return {"info": f"Code {code} activated successfully!"}
