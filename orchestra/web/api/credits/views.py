import logging
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Request
from fastapi.param_functions import Depends

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import BillingAccount
from orchestra.lib.billing import get_billing_entity, queue_auto_recharge
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
            "description": "Invalid request (billing not set up)",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Billing is not set up",
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

    The amount must be positive. The balance is allowed to go negative so
    that the spending-limit hook (which checks ``credit_balance <= 0``)
    will correctly block subsequent LLM calls. If auto-recharge is
    configured, it is triggered after the deduction.
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

    new_balance = billing_deduct_credits(
        session,
        billing_entity,
        Decimal(str(request.amount)),
    )

    # Trigger auto-recharge if credits fell below threshold
    if billing_entity.should_trigger_autorecharge(new_balance):
        ba = (
            session.query(BillingAccount)
            .filter(BillingAccount.id == billing_entity.billing_account_id)
            .first()
        )
        if ba:
            recharged = queue_auto_recharge(
                session,
                ba,
                int(billing_entity.autorecharge_qty),
                entity_label=(
                    f"user {billing_entity.entity_id}"
                    if billing_entity.is_user
                    else f"org {billing_entity.entity_id}"
                ),
            )
            if recharged:
                new_balance = ba.credits

    session.commit()

    return DeductCreditsResponse(
        previous_credits=current_credits,
        deducted=request.amount,
        current_credits=float(new_balance),
    )
