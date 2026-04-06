import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.param_functions import Depends

from orchestra.db.dao.resource_access_dao import ResourceAccessDAO
from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import BillingAccount
from orchestra.lib.billing import get_billing_entity, queue_auto_recharge
from orchestra.web.api.credits.schema import (
    CreditsResponse,
    DeductCreditsRequest,
    DeductCreditsResponse,
    SpendingBreakdownResponse,
    TransactionHistoryResponse,
    TransactionItem,
)

router = APIRouter()

logger = logging.getLogger(__name__)


def _check_org_billing_permission(session, user_id, organization_id, permission):
    if organization_id is None:
        return
    ra_dao = ResourceAccessDAO(session)
    if not ra_dao.check_user_has_permission_in_org(
        user_id,
        organization_id,
        permission,
    ):
        raise HTTPException(
            status_code=403,
            detail=f"You do not have {permission} permission in this organization",
        )


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

    _check_org_billing_permission(session, user_id, organization_id, "billing:read")

    try:
        billing_entity = get_billing_entity(session, user_id, organization_id)
        credits = float(billing_entity.credits)
    except ValueError:
        credits = 0.0

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

    _check_org_billing_permission(session, user_id, organization_id, "billing:write")

    try:
        billing_entity = get_billing_entity(session, user_id, organization_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Billing is not set up")

    current_credits = float(billing_entity.credits)

    new_balance = billing_deduct_credits(
        session,
        billing_entity,
        Decimal(str(request.amount)),
        category=request.category,
        assistant_id=request.assistant_id,
        user_id=request.user_id or user_id,
        organization_id=organization_id,
        description=request.description,
        detail=request.detail,
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


@router.get(
    "/credits/transactions",
    response_model=TransactionHistoryResponse,
)
def get_transaction_history(
    request_fastapi: Request,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    category: Optional[str] = Query(None),
    assistant_id: Optional[int] = Query(None),
    filter_user_id: Optional[str] = Query(None, alias="user_id"),
    start_date: Optional[str] = Query(None, regex=r"^\d{4}-\d{2}-\d{2}"),
    end_date: Optional[str] = Query(None, regex=r"^\d{4}-\d{2}-\d{2}"),
    session=Depends(get_db_session),
) -> TransactionHistoryResponse:
    """Paginated credit transaction history for the current billing account.

    Filters are scoped to the billing account resolved from the API key.
    Use ``user_id`` to see spending by a specific member in an org context.
    Use ``start_date`` / ``end_date`` (ISO date strings) to restrict the
    time window (inclusive start, exclusive end).
    """
    from orchestra.db.dao.credit_transaction_dao import CreditTransactionDAO

    user_id = request_fastapi.state.user_id
    organization_id = getattr(request_fastapi.state, "organization_id", None)

    _check_org_billing_permission(session, user_id, organization_id, "billing:read")

    try:
        billing_entity = get_billing_entity(session, user_id, organization_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Billing is not set up")

    since = None
    until = None
    if start_date:
        since = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if end_date:
        until = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    txn_dao = CreditTransactionDAO(session)
    rows = txn_dao.get_transactions(
        billing_entity.billing_account_id,
        limit=limit,
        offset=offset,
        category=category,
        assistant_id=assistant_id,
        user_id=filter_user_id,
        since=since,
        until=until,
    )

    return TransactionHistoryResponse(
        transactions=[
            TransactionItem(
                id=r.id,
                at=r.at,
                amount=float(r.amount),
                balance_after=(
                    float(r.balance_after) if r.balance_after is not None else None
                ),
                category=r.category,
                assistant_id=r.assistant_id,
                user_id=r.user_id,
                organization_id=r.organization_id,
                description=r.description,
                detail=r.detail,
            )
            for r in rows
        ],
    )


@router.get(
    "/credits/spending",
    response_model=SpendingBreakdownResponse,
)
def get_spending_breakdown(
    request_fastapi: Request,
    month: Optional[str] = Query(None, regex=r"^\d{4}-\d{2}$"),
    assistant_id: Optional[int] = Query(None),
    filter_user_id: Optional[str] = Query(None, alias="user_id"),
    session=Depends(get_db_session),
) -> SpendingBreakdownResponse:
    """Monthly spending breakdown by category, queried from the credit ledger.

    Uses the composite index ``(billing_account_id, category, at)``
    so aggregation only touches relevant rows.

    Use ``user_id`` to see spending by a specific member in an org context.
    """
    from orchestra.db.dao.credit_transaction_dao import CreditTransactionDAO

    user_id = request_fastapi.state.user_id
    organization_id = getattr(request_fastapi.state, "organization_id", None)

    _check_org_billing_permission(session, user_id, organization_id, "billing:read")

    try:
        billing_entity = get_billing_entity(session, user_id, organization_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Billing is not set up")

    if month is None:
        month = datetime.now(timezone.utc).strftime("%Y-%m")

    year, mon = map(int, month.split("-"))
    month_start = datetime(year, mon, 1, tzinfo=timezone.utc)
    if mon == 12:
        month_end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        month_end = datetime(year, mon + 1, 1, tzinfo=timezone.utc)

    txn_dao = CreditTransactionDAO(session)
    breakdown = txn_dao.get_spending_by_category(
        billing_entity.billing_account_id,
        month_start,
        month_end,
        assistant_id=assistant_id,
        user_id=filter_user_id,
    )
    total = sum(breakdown.values())

    return SpendingBreakdownResponse(
        month=month,
        total=total,
        by_category=breakdown,
    )
