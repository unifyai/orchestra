"""Ownership-scoped task-machine routes for the assistant runtime.

User-API-key equivalents of the ``/admin/task-run/*`` and
``/admin/task-outbound-operation/*`` routes: identical request/response
shapes, but the caller must own the ``assistant_id`` referenced in the
payload (system/admin keys bypass the check via ``require_owned_assistant``).
"""

import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from orchestra.db.dao.provider_trigger_dao import ProviderTriggerDAO
from orchestra.db.dependencies import get_db_session
from orchestra.provider_triggers.dispatch_request import EVENT_CONTEXT_AUDIENCE
from orchestra.provider_triggers.private_event_storage import (
    EventBlobAuthenticationError,
)
from orchestra.services.provider_event_blob_service import ProviderEventBlobService
from orchestra.services.task_machine_state_service import (
    TASK_MACHINE_PROJECT_NAME,
    get_task_run_by_run_id,
)
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

_EVENT_CONTEXT_AUDIENCE_REJECTED = "invalid_event_context_audience"
_EVENT_CONTEXT_UNAVAILABLE = "event_context_unavailable"


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
            detail=_EVENT_CONTEXT_AUDIENCE_REJECTED,
        )
    _require_owned_task_assistant(request_fastapi, request.assistant_id, session)
    actor = getattr(request_fastapi.state, "user_id", None) or "unity"
    return _read_provider_event_context(session, request, actor=actor)


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


def _read_provider_event_context(
    session,
    request: ProviderEventContextRequest,
    *,
    actor: str,
) -> ProviderEventContextResponse:
    """Resolve, authorize, and decrypt one provider-event context bundle.

    The audience is validated by the caller. Every failure below collapses to
    the same 404 so ownership, missing rows, receipt drift, and storage errors
    are indistinguishable to the client.
    """

    try:
        assistant_id = int(str(request.assistant_id))
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=404,
            detail=_EVENT_CONTEXT_UNAVAILABLE,
        ) from exc

    project = _get_internal_project_or_404(
        session,
        project_name=TASK_MACHINE_PROJECT_NAME,
        assistant_id=request.assistant_id,
    )
    run = get_task_run_by_run_id(session, project.id, run_id=request.run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=_EVENT_CONTEXT_UNAVAILABLE)
    run_data = dict(run.data or {})
    if str(run_data.get("task_id")) != str(request.task_id):
        raise HTTPException(status_code=404, detail=_EVENT_CONTEXT_UNAVAILABLE)
    run_assistant_id = run_data.get("assistant_id")
    if run_assistant_id is not None and str(run_assistant_id) != str(
        request.assistant_id,
    ):
        raise HTTPException(status_code=404, detail=_EVENT_CONTEXT_UNAVAILABLE)

    _, receipt, _ = ProviderTriggerDAO(session).get_event_blob_for_authorized_read(
        assistant_id=assistant_id,
        task_id=request.task_id,
        receipt_id=request.receipt_id,
    )
    if receipt is None:
        raise HTTPException(status_code=404, detail=_EVENT_CONTEXT_UNAVAILABLE)
    if (
        not receipt.event_context_ref
        or receipt.event_context_ref != request.event_context_ref
    ):
        raise HTTPException(status_code=404, detail=_EVENT_CONTEXT_UNAVAILABLE)

    try:
        source_bytes = ProviderEventBlobService(session).read_authorized(
            assistant_id=assistant_id,
            task_id=request.task_id,
            receipt_id=request.receipt_id,
            actor=actor,
            audience=request.audience,
        )
    except (PermissionError, EventBlobAuthenticationError) as exc:
        raise HTTPException(
            status_code=404,
            detail=_EVENT_CONTEXT_UNAVAILABLE,
        ) from exc

    return ProviderEventContextResponse(
        receipt_id=receipt.receipt_id,
        run_id=request.run_id,
        event_context_ref=receipt.event_context_ref,
        envelope=dict(receipt.stable_envelope_json or {}),
        curated_projection=dict(receipt.curated_projection_json or {}),
        source_body=_parse_source_body(source_bytes),
    )


def _parse_source_body(raw: bytes):
    """Return the decrypted body as parsed JSON, falling back to text."""

    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return raw.decode("utf-8", errors="replace")
