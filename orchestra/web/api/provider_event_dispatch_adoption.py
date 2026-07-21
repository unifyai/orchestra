"""Authenticated claim and report routes for provider-event dispatch adoption."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from orchestra.db.dao.provider_trigger_dao import ProviderTriggerDAO
from orchestra.db.dependencies import get_db_session
from orchestra.services.provider_event_dispatch_adoption_service import (
    DispatchAdoptionError,
    DispatchAuthorizationSnapshot,
    ProviderEventDispatchAdoptionService,
)
from orchestra.web.api.dependencies import auth_admin_key
from orchestra.web.api.utils.assistant_ownership import require_owned_assistant

admin_router = APIRouter()
user_router = APIRouter()


class ProviderEventDispatchClaimRequest(BaseModel):
    """Compare-and-set claim for one immutable dispatch operation."""

    model_config = ConfigDict(extra="forbid")

    operation_id: str
    run_id: int
    run_key: str
    assistant_id: str
    task_id: int
    binding_id: str
    receipt_id: str
    accepted_revision: str
    dispatch_mode: Literal["live", "offline"]
    audience: str
    claimant_id: str = Field(
        description="Stable identity of the rail process requesting launch ownership.",
    )
    launch_identity: str | None = Field(
        default=None,
        description="Deterministic sink identity known before launch I/O.",
    )


class ProviderEventDispatchReportStartedRequest(BaseModel):
    """Fenced start report for one dispatch operation."""

    model_config = ConfigDict(extra="forbid")

    operation_id: str
    fencing_token: int
    launch_identity: str | None = None


class ProviderEventDispatchReportTerminalRequest(BaseModel):
    """Fenced terminal report for one dispatch operation."""

    model_config = ConfigDict(extra="forbid")

    operation_id: str
    fencing_token: int
    terminal_reason: str
    launch_identity: str | None = None


class ProviderEventDispatchAdoptionResponse(BaseModel):
    """Public adoption state owned by Orchestra."""

    model_config = ConfigDict(extra="forbid")

    operation_id: str
    run_id: int
    run_key: str
    status: Literal["adopted", "started", "terminal"]
    fencing_token: int
    owns_launch: bool = False
    launch_identity: str | None = None
    terminal_reason: str | None = None


def _authorization_snapshot(
    request: ProviderEventDispatchClaimRequest,
) -> DispatchAuthorizationSnapshot:
    try:
        assistant_id = int(str(request.assistant_id))
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail={"reason": "invalid_assistant_id"},
        ) from exc
    return DispatchAuthorizationSnapshot(
        operation_id=request.operation_id,
        run_id=request.run_id,
        run_key=request.run_key,
        assistant_id=assistant_id,
        task_id=request.task_id,
        binding_id=request.binding_id,
        receipt_id=request.receipt_id,
        accepted_revision=request.accepted_revision,
        dispatch_mode=request.dispatch_mode,
        audience=request.audience,
    )


def _raise_adoption_http(exc: DispatchAdoptionError) -> None:
    status_code = 404
    if exc.reason_code in {
        "dispatch_authorization_mismatch",
        "fencing_token_mismatch",
        "dispatch_already_terminal",
        "dispatch_already_started",
    }:
        status_code = 409
    elif exc.reason_code in {"claimant_id_required", "terminal_reason_required"}:
        status_code = 400
    raise HTTPException(
        status_code=status_code,
        detail={"reason": exc.reason_code},
    ) from exc


def _claim_response(result) -> ProviderEventDispatchAdoptionResponse:
    return ProviderEventDispatchAdoptionResponse(
        operation_id=result.operation_id,
        run_id=result.run_id,
        run_key=result.run_key,
        status=result.status,  # type: ignore[arg-type]
        fencing_token=result.fencing_token,
        owns_launch=getattr(result, "owns_launch", False),
        launch_identity=result.launch_identity,
        terminal_reason=result.terminal_reason,
    )


def _require_owned_assistant_id(
    request_fastapi: Request,
    assistant_id: str,
    session: Session,
) -> None:
    if getattr(request_fastapi.state, "is_system_api_key", False):
        return
    try:
        agent_id = int(str(assistant_id))
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=404,
            detail=f"Assistant '{assistant_id}' not found.",
        ) from exc
    require_owned_assistant(request_fastapi, agent_id, session, write=True)


def _claim_core(
    request: ProviderEventDispatchClaimRequest,
    session: Session,
) -> ProviderEventDispatchAdoptionResponse:
    service = ProviderEventDispatchAdoptionService(session)
    try:
        result = service.claim(
            authorization=_authorization_snapshot(request),
            claimant_id=request.claimant_id,
            launch_identity=request.launch_identity,
        )
    except DispatchAdoptionError as exc:
        _raise_adoption_http(exc)
    session.commit()
    return _claim_response(result)


def _report_started_core(
    request: ProviderEventDispatchReportStartedRequest,
    session: Session,
) -> ProviderEventDispatchAdoptionResponse:
    service = ProviderEventDispatchAdoptionService(session)
    try:
        result = service.report_started(
            operation_id=request.operation_id,
            fencing_token=request.fencing_token,
            launch_identity=request.launch_identity,
        )
    except DispatchAdoptionError as exc:
        _raise_adoption_http(exc)
    session.commit()
    return _claim_response(result)


def _report_terminal_core(
    request: ProviderEventDispatchReportTerminalRequest,
    session: Session,
) -> ProviderEventDispatchAdoptionResponse:
    service = ProviderEventDispatchAdoptionService(session)
    try:
        result = service.report_terminal(
            operation_id=request.operation_id,
            fencing_token=request.fencing_token,
            terminal_reason=request.terminal_reason,
            launch_identity=request.launch_identity,
        )
    except DispatchAdoptionError as exc:
        _raise_adoption_http(exc)
    session.commit()
    return _claim_response(result)


def _get_status_core(
    operation_id: str,
    session: Session,
) -> ProviderEventDispatchAdoptionResponse:
    service = ProviderEventDispatchAdoptionService(session)
    try:
        result = service.get_public_status(operation_id=operation_id)
    except DispatchAdoptionError as exc:
        _raise_adoption_http(exc)
    return _claim_response(result)


@admin_router.post(
    "/provider-event-dispatch/claim",
    response_model=ProviderEventDispatchAdoptionResponse,
)
def admin_claim_provider_event_dispatch(
    request: ProviderEventDispatchClaimRequest,
    session: Session = Depends(get_db_session),
    _: str = Depends(auth_admin_key),
) -> ProviderEventDispatchAdoptionResponse:
    """Claim launch ownership for one offline or live dispatch operation."""

    return _claim_core(request, session)


@admin_router.post(
    "/provider-event-dispatch/report-started",
    response_model=ProviderEventDispatchAdoptionResponse,
)
def admin_report_provider_event_dispatch_started(
    request: ProviderEventDispatchReportStartedRequest,
    session: Session = Depends(get_db_session),
    _: str = Depends(auth_admin_key),
) -> ProviderEventDispatchAdoptionResponse:
    """Report fenced start for one dispatch operation."""

    return _report_started_core(request, session)


@admin_router.post(
    "/provider-event-dispatch/report-terminal",
    response_model=ProviderEventDispatchAdoptionResponse,
)
def admin_report_provider_event_dispatch_terminal(
    request: ProviderEventDispatchReportTerminalRequest,
    session: Session = Depends(get_db_session),
    _: str = Depends(auth_admin_key),
) -> ProviderEventDispatchAdoptionResponse:
    """Report fenced terminal failure for one dispatch operation."""

    return _report_terminal_core(request, session)


@admin_router.get(
    "/provider-event-dispatch/{operation_id}/adoption",
    response_model=ProviderEventDispatchAdoptionResponse,
)
def admin_get_provider_event_dispatch_adoption(
    operation_id: str,
    session: Session = Depends(get_db_session),
    _: str = Depends(auth_admin_key),
) -> ProviderEventDispatchAdoptionResponse:
    """Return Orchestra's durable public adoption status."""

    return _get_status_core(operation_id, session)


@user_router.post(
    "/provider-event-dispatch/claim",
    response_model=ProviderEventDispatchAdoptionResponse,
    include_in_schema=False,
)
def claim_provider_event_dispatch(
    request: ProviderEventDispatchClaimRequest,
    request_fastapi: Request,
    session: Session = Depends(get_db_session),
) -> ProviderEventDispatchAdoptionResponse:
    """Ownership-scoped claim for live Unity dispatch adoption."""

    _require_owned_assistant_id(request_fastapi, request.assistant_id, session)
    return _claim_core(request, session)


@user_router.post(
    "/provider-event-dispatch/report-started",
    response_model=ProviderEventDispatchAdoptionResponse,
    include_in_schema=False,
)
def report_provider_event_dispatch_started(
    request: ProviderEventDispatchReportStartedRequest,
    request_fastapi: Request,
    session: Session = Depends(get_db_session),
) -> ProviderEventDispatchAdoptionResponse:
    """Ownership-scoped fenced start report for live Unity dispatch."""

    dispatch = ProviderTriggerDAO(session).get_dispatch_by_operation_id(
        operation_id=request.operation_id,
    )
    if dispatch is None:
        raise HTTPException(
            status_code=404,
            detail={"reason": "provider_event_dispatch_not_found"},
        )
    _require_owned_assistant_id(
        request_fastapi,
        str(dispatch.assistant_id),
        session,
    )
    return _report_started_core(request, session)


@user_router.post(
    "/provider-event-dispatch/report-terminal",
    response_model=ProviderEventDispatchAdoptionResponse,
    include_in_schema=False,
)
def report_provider_event_dispatch_terminal(
    request: ProviderEventDispatchReportTerminalRequest,
    request_fastapi: Request,
    session: Session = Depends(get_db_session),
) -> ProviderEventDispatchAdoptionResponse:
    """Ownership-scoped fenced terminal report for live Unity dispatch."""

    dispatch = ProviderTriggerDAO(session).get_dispatch_by_operation_id(
        operation_id=request.operation_id,
    )
    if dispatch is None:
        raise HTTPException(
            status_code=404,
            detail={"reason": "provider_event_dispatch_not_found"},
        )
    _require_owned_assistant_id(
        request_fastapi,
        str(dispatch.assistant_id),
        session,
    )
    return _report_terminal_core(request, session)


@user_router.get(
    "/provider-event-dispatch/{operation_id}/adoption",
    response_model=ProviderEventDispatchAdoptionResponse,
    include_in_schema=False,
)
def get_provider_event_dispatch_adoption(
    operation_id: str,
    request_fastapi: Request,
    session: Session = Depends(get_db_session),
) -> ProviderEventDispatchAdoptionResponse:
    """Ownership-scoped public adoption status for one operation."""

    dispatch = ProviderTriggerDAO(session).get_dispatch_by_operation_id(
        operation_id=operation_id,
    )
    if dispatch is None:
        raise HTTPException(
            status_code=404,
            detail={"reason": "provider_event_dispatch_not_found"},
        )
    _require_owned_assistant_id(
        request_fastapi,
        str(dispatch.assistant_id),
        session,
    )
    return _get_status_core(operation_id, session)
