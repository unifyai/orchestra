"""Admin diagnostics for provider-event dispatch operations."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from orchestra.db.dao.provider_trigger_dao import ProviderTriggerDAO
from orchestra.db.dependencies import get_db_session
from orchestra.services.provider_event_dispatch_delivery_service import (
    ProviderEventDispatchDeliveryService,
)
from orchestra.settings import settings
from orchestra.web.api.dependencies import auth_admin_key

router = APIRouter()


class ProviderEventDispatchRepairRequest(BaseModel):
    """Requeue one non-terminal dispatch operation for worker delivery."""

    operation_id: str = Field(description="Immutable dispatch operation identifier.")


class ProviderEventDispatchProcessBacklogRequest(BaseModel):
    """Run bounded dispatch delivery and convergence batches."""

    delivery_batch_size: int | None = Field(
        default=None,
        ge=1,
        le=500,
        description="Optional override for the delivery claim batch size.",
    )
    convergence_batch_size: int | None = Field(
        default=None,
        ge=1,
        le=500,
        description="Optional override for the convergence poll batch size.",
    )


def _serialize_dispatch(dispatch) -> dict:
    return {
        "operation_id": dispatch.operation_id,
        "receipt_id": dispatch.receipt_id,
        "binding_id": dispatch.binding_id,
        "assistant_id": dispatch.assistant_id,
        "task_id": dispatch.task_id,
        "run_id": dispatch.run_id,
        "run_key": dispatch.run_key,
        "dispatch_mode": dispatch.dispatch_mode,
        "accepted_activation_revision": dispatch.accepted_activation_revision,
        "event_context_ref": dispatch.event_context_ref,
        "audience": dispatch.audience,
        "processing_state": dispatch.processing_state,
        "attempt_count": dispatch.attempt_count,
        "next_retry_at": dispatch.next_retry_at,
        "lease_owner": dispatch.lease_owner,
        "lease_expires_at": dispatch.lease_expires_at,
        "downstream_adoption_status": dispatch.downstream_adoption_status,
        "downstream_adoption_ref": dispatch.downstream_adoption_ref,
        "downstream_status_at": dispatch.downstream_status_at,
        "terminal_error_code": dispatch.terminal_error_code,
        "delivered_at": dispatch.delivered_at,
        "started_at": dispatch.started_at,
        "terminal_at": dispatch.terminal_at,
        "created_at": dispatch.created_at,
        "updated_at": dispatch.updated_at,
    }


@router.get("/provider-event-dispatch/{operation_id}")
def get_provider_event_dispatch(
    operation_id: str,
    session: Session = Depends(get_db_session),
    _: str = Depends(auth_admin_key),
) -> dict:
    """Return one dispatch operation and its linked receipt summary."""

    dao = ProviderTriggerDAO(session)
    dispatch = dao.get_dispatch_by_operation_id(operation_id=operation_id)
    if dispatch is None:
        raise HTTPException(status_code=404, detail="provider_event_dispatch_not_found")

    receipt = dao.get_receipt_by_id(receipt_id=dispatch.receipt_id)
    receipt_summary = None
    if receipt is not None:
        receipt_summary = {
            "receipt_id": receipt.receipt_id,
            "processing_state": receipt.processing_state,
            "classification_reason": receipt.classification_reason,
            "received_at": receipt.received_at,
            "terminal_at": receipt.terminal_at,
            "terminal_reason": receipt.terminal_reason,
        }
    return {
        "dispatch": _serialize_dispatch(dispatch),
        "receipt": receipt_summary,
    }


@router.post("/provider-event-dispatch/repair")
def repair_provider_event_dispatch(
    request: ProviderEventDispatchRepairRequest,
    session: Session = Depends(get_db_session),
    _: str = Depends(auth_admin_key),
) -> dict:
    """Requeue one non-terminal dispatch operation."""

    dao = ProviderTriggerDAO(session)
    dispatch = dao.get_dispatch_by_operation_id(
        operation_id=request.operation_id,
        for_update=True,
    )
    if dispatch is None:
        raise HTTPException(status_code=404, detail="provider_event_dispatch_not_found")
    try:
        repaired = dao.requeue_dispatch_for_repair(dispatch=dispatch)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    session.commit()
    return {"dispatch": _serialize_dispatch(repaired)}


@router.post("/provider-event-dispatch/process-backlog")
def process_provider_event_dispatch_backlog(
    request: ProviderEventDispatchProcessBacklogRequest,
    session: Session = Depends(get_db_session),
    _: str = Depends(auth_admin_key),
) -> dict:
    """Run bounded dispatch delivery and convergence batches."""

    delivery_batch_size = (
        request.delivery_batch_size or settings.provider_trigger_dispatch_batch_size
    )
    convergence_batch_size = (
        request.convergence_batch_size
        or settings.provider_trigger_dispatch_status_batch_size
    )
    service = ProviderEventDispatchDeliveryService(session)
    delivery_stats = service.process_dispatch_batch(batch_size=delivery_batch_size)
    converge_stats = service.process_status_convergence_batch(
        batch_size=convergence_batch_size,
    )
    session.commit()

    return {
        "processed_at": datetime.now(timezone.utc),
        "delivery": delivery_stats,
        "convergence": converge_stats,
        "backlog_oldest_age_seconds": service.backlog_oldest_age_seconds(),
    }
