"""Ownership-scoped task-machine routes for the assistant runtime.

User-API-key equivalents of the ``/admin/task-run/*`` and
``/admin/task-outbound-operation/*`` routes: identical request/response
shapes, but the caller must own the ``assistant_id`` referenced in the
payload (system/admin keys bypass the check via ``require_owned_assistant``).
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from orchestra.db.dependencies import get_db_session
from orchestra.provider_triggers.dispatch_request import EVENT_CONTEXT_AUDIENCE
from orchestra.provider_triggers.event_context_errors import (
    EventContextAccessError,
    EventContextErrorReason,
)
from orchestra.services.provider_event_context_service import (
    ProviderEventContextService,
    ResolvedEventContext,
)
from orchestra.services.task_machine_state_service import TASK_MACHINE_PROJECT_NAME
from orchestra.web.api.log.task_machine_admin import (
    _get_internal_project_or_404,
    create_or_adopt_task_outbound_operation_core,
    create_or_adopt_task_run_core,
    get_latest_task_run_core,
    get_task_run_core,
    patch_task_outbound_operation_core,
    patch_task_run_core,
)
from orchestra.web.api.log.task_machine_schema import (
    ProviderEventContextRequest,
    ProviderEventContextResponse,
    TaskOutboundOperationCreateOrAdoptRequest,
    TaskOutboundOperationMutationResponse,
    TaskOutboundOperationUpdateRequest,
    TaskRunCreateOrAdoptRequest,
    TaskRunGetRequest,
    TaskRunGetResponse,
    TaskRunLatestRequest,
    TaskRunLatestResponse,
    TaskRunMutationResponse,
    TaskRunUpdateRequest,
)
from orchestra.web.api.utils.assistant_ownership import require_owned_assistant

router = APIRouter()


def _event_context_response(
    bundle: ResolvedEventContext,
) -> ProviderEventContextResponse:
    return ProviderEventContextResponse(
        receipt_id=bundle.receipt_id,
        run_id=bundle.run_id,
        event_context_ref=bundle.event_context_ref,
        envelope=bundle.envelope,
        curated_projection=bundle.curated_projection,
        source_body=bundle.source_body,
        expires_at=bundle.expires_at,
    )


def _raise_event_context_http(exc: EventContextAccessError) -> None:
    raise HTTPException(status_code=404, detail=exc.reason.value) from exc


def _require_owned_task_assistant(
    request_fastapi: Request,
    assistant_id: str,
    session,
) -> None:
    """Enforce ownership of the ``assistant_id`` carried in a task-machine payload.

    Task-machine payloads carry the assistant identifier as a string; user-key
    callers must resolve it to an integer agent id they own. System (admin-key)
    callers bypass the check entirely, preserving the admin routes' non-numeric
    test fallbacks.
    """
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


@router.post(
    "/task-run/create-or-adopt",
    response_model=TaskRunMutationResponse,
    include_in_schema=False,
)
def create_or_adopt_task_run(
    request: TaskRunCreateOrAdoptRequest,
    request_fastapi: Request,
    session=Depends(get_db_session),
):
    """Create a task run by run_key if absent, otherwise return the existing row."""

    _require_owned_task_assistant(request_fastapi, request.assistant_id, session)
    return create_or_adopt_task_run_core(session, request)


@router.post(
    "/task-run/update",
    response_model=TaskRunMutationResponse,
    include_in_schema=False,
)
def patch_task_run(
    request: TaskRunUpdateRequest,
    request_fastapi: Request,
    session=Depends(get_db_session),
):
    """Apply a partial payload update to an existing task run row."""

    _require_owned_task_assistant(request_fastapi, request.assistant_id, session)
    return patch_task_run_core(session, request)


@router.post(
    "/task-run/latest",
    response_model=TaskRunLatestResponse,
    include_in_schema=False,
)
def get_latest_task_run(
    request: TaskRunLatestRequest,
    request_fastapi: Request,
    session=Depends(get_db_session),
):
    """Return the most recently updated task run for one assistant/task pair."""

    _require_owned_task_assistant(request_fastapi, request.assistant_id, session)
    return get_latest_task_run_core(session, request)


@router.get(
    "/task-run/latest",
    response_model=TaskRunLatestResponse,
    include_in_schema=False,
)
def get_latest_task_run_by_params(
    request_fastapi: Request,
    assistant_id: str = Query(..., description="Assistant that owns the run."),
    task_id: int = Query(..., description="Logical task identifier."),
    project_name: str = Query(
        TASK_MACHINE_PROJECT_NAME,
        description="Project that owns the internal task machine contexts.",
    ),
    source_task_log_id: Optional[int] = Query(
        None,
        description="Optional Tasks row log id used to resolve assistant-scoped contexts.",
    ),
    session=Depends(get_db_session),
):
    """Query-parameter variant of the latest-task-run lookup."""

    _require_owned_task_assistant(request_fastapi, assistant_id, session)
    request = TaskRunLatestRequest(
        project_name=project_name,
        assistant_id=assistant_id,
        task_id=task_id,
        source_task_log_id=source_task_log_id,
    )
    return get_latest_task_run_core(session, request)


@router.post(
    "/task-run/get",
    response_model=TaskRunGetResponse,
    include_in_schema=False,
)
def get_task_run_by_key(
    request: TaskRunGetRequest,
    request_fastapi: Request,
    session=Depends(get_db_session),
):
    """Return one task run row by run_key without creating or adopting."""

    _require_owned_task_assistant(request_fastapi, request.assistant_id, session)
    return get_task_run_core(session, request)


@router.post(
    "/provider-event/event-context",
    response_model=ProviderEventContextResponse,
    include_in_schema=False,
)
def get_provider_event_context(
    request: ProviderEventContextRequest,
    request_fastapi: Request,
    session=Depends(get_db_session),
):
    """Return one accepted provider-event's decrypted context for an owned run.

    Fails closed with a 404 and a stable ``event_context_unavailable`` reason
    whenever ownership, the run/task linkage, the receipt reference, or the
    encrypted blob cannot be resolved, so no backend detail leaks to callers.
    """

    if request.audience != EVENT_CONTEXT_AUDIENCE:
        raise HTTPException(
            status_code=400,
            detail=EventContextErrorReason.invalid_audience.value,
        )
    _require_owned_task_assistant(request_fastapi, request.assistant_id, session)
    actor = getattr(request_fastapi.state, "user_id", None) or "unity"
    project = _get_internal_project_or_404(
        session,
        project_name=TASK_MACHINE_PROJECT_NAME,
        assistant_id=request.assistant_id,
    )
    try:
        assistant_id = int(str(request.assistant_id))
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=404,
            detail=EventContextErrorReason.unavailable.value,
        ) from exc
    try:
        bundle = ProviderEventContextService(session).read_for_service(
            project_id=project.id,
            assistant_id=assistant_id,
            task_id=request.task_id,
            run_id=request.run_id,
            receipt_id=request.receipt_id,
            event_context_ref=request.event_context_ref,
            audience=request.audience,
            issued_at=request.issued_at,
            actor=actor,
        )
    except EventContextAccessError as exc:
        _raise_event_context_http(exc)
    return _event_context_response(bundle)


@router.post(
    "/task-outbound-operation/create-or-adopt",
    response_model=TaskOutboundOperationMutationResponse,
    include_in_schema=False,
)
def create_or_adopt_task_outbound_operation(
    request: TaskOutboundOperationCreateOrAdoptRequest,
    request_fastapi: Request,
    session=Depends(get_db_session),
):
    """Create an outbound operation by operation_key if absent, otherwise adopt it."""

    _require_owned_task_assistant(request_fastapi, request.assistant_id, session)
    return create_or_adopt_task_outbound_operation_core(session, request)


@router.post(
    "/task-outbound-operation/update",
    response_model=TaskOutboundOperationMutationResponse,
    include_in_schema=False,
)
def patch_task_outbound_operation(
    request: TaskOutboundOperationUpdateRequest,
    request_fastapi: Request,
    session=Depends(get_db_session),
):
    """Apply a partial payload update to an existing outbound operation row."""

    _require_owned_task_assistant(request_fastapi, request.assistant_id, session)
    return patch_task_outbound_operation_core(session, request)


def read_owned_task_event_context(
    session,
    *,
    project_id: int,
    assistant_id: int,
    task_id: int,
    run_id: int,
    actor: str,
    export: bool = False,
) -> ProviderEventContextResponse:
    """Return one owned provider-event context bundle."""

    service = ProviderEventContextService(session)
    try:
        if export:
            bundle = service.export_for_user(
                project_id=project_id,
                assistant_id=assistant_id,
                task_id=task_id,
                run_id=run_id,
                actor=actor,
            )
        else:
            bundle = service.read_for_user(
                project_id=project_id,
                assistant_id=assistant_id,
                task_id=task_id,
                run_id=run_id,
                actor=actor,
            )
    except EventContextAccessError as exc:
        _raise_event_context_http(exc)
    return _event_context_response(bundle)


def delete_owned_task_event_context(
    session,
    *,
    project_id: int,
    assistant_id: int,
    task_id: int,
    run_id: int,
    actor: str,
) -> None:
    """Delete one owned provider-event context bundle."""

    service = ProviderEventContextService(session)
    try:
        service.delete_for_user(
            project_id=project_id,
            assistant_id=assistant_id,
            task_id=task_id,
            run_id=run_id,
            actor=actor,
        )
    except EventContextAccessError as exc:
        _raise_event_context_http(exc)
