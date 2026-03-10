import logging
from decimal import Decimal
from typing import Dict

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.param_functions import Depends

from orchestra.db.dependencies import get_db_session
from orchestra.lib.billing import get_billing_entity
from orchestra.web.api.credits.schema import (
    CreditsResponse,
    DeductCreditsRequest,
    DeductCreditsResponse,
)

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

