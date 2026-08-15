"""Admin fact exports for the company KPI dashboard.

Four keyset-paginated, read-only routes under ``/admin/metrics/export``.
They return facts — users, payments, per-day activity, plan history — and
leave every KPI to the consumer, so Orchestra never carries a metric
definition that can drift from the dashboard's. Query logic lives in
:mod:`orchestra.routines.kpi_export`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Callable, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.enums import RechargeStatus
from orchestra.routines import kpi_export
from orchestra.routines.kpi_export import InvalidCursor

router = APIRouter()

Since = Annotated[
    Optional[datetime],
    Query(description="ISO-8601, interpreted as UTC. Inclusive lower bound."),
]
Until = Annotated[
    Optional[datetime],
    Query(description="ISO-8601, interpreted as UTC. Exclusive upper bound."),
]
Cursor = Annotated[
    Optional[str],
    Query(description="Opaque ``next_cursor`` from the previous page."),
]
Limit = Annotated[int, Query(ge=1, le=1000, description="Rows per page.")]

DEFAULT_PAYMENT_STATUSES = (
    f"{RechargeStatus.PAID.value},{RechargeStatus.DISPUTED.value}"
)


def _paged(export: Callable[..., dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
    try:
        return export(**kwargs)
    except InvalidCursor as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _recharge_statuses(raw: str) -> list[str]:
    statuses = [token.strip().upper() for token in raw.split(",") if token.strip()]
    unknown = sorted(set(statuses) - {status.value for status in RechargeStatus})
    if not statuses or unknown:
        raise HTTPException(
            status_code=422,
            detail=f"statuses must be a comma list of RechargeStatus values; got {raw!r}",
        )
    return statuses


@router.get("/export/users")
def export_users(
    since: Since = None,
    until: Until = None,
    cursor: Cursor = None,
    limit: Limit = 500,
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """Users with signup provenance, keyed on ``(created_at, id)``."""
    return _paged(
        kpi_export.export_users,
        session=session,
        since=since,
        until=until,
        cursor=cursor,
        limit=limit,
    )


@router.get("/export/payments")
def export_payments(
    since: Since = None,
    until: Until = None,
    statuses: Annotated[
        str,
        Query(description="Comma list of RechargeStatus values."),
    ] = DEFAULT_PAYMENT_STATUSES,
    cursor: Cursor = None,
    limit: Limit = 500,
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """Money-carrying recharges, keyed on ``(at, id)``."""
    return _paged(
        kpi_export.export_payments,
        session=session,
        since=since,
        until=until,
        statuses=_recharge_statuses(statuses),
        cursor=cursor,
        limit=limit,
    )


@router.get("/export/activity")
def export_activity(
    since: Since = None,
    until: Until = None,
    cursor: Cursor = None,
    limit: Limit = 500,
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """Per-(user, UTC day) billable debit rollups, keyed on ``(day, user_id)``."""
    return _paged(
        kpi_export.export_activity,
        session=session,
        since=since,
        until=until,
        cursor=cursor,
        limit=limit,
    )


@router.get("/export/plans")
def export_plans(
    since: Since = None,
    cursor: Cursor = None,
    limit: Limit = 500,
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """Plan-assignment history with its template, keyed on ``(started_at, id)``."""
    return _paged(
        kpi_export.export_plans,
        session=session,
        since=since,
        cursor=cursor,
        limit=limit,
    )
