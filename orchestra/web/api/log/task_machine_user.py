"""Ownership-scoped task-machine routes for the assistant runtime.

User-API-key equivalents of the ``/admin/task-run/*`` and
``/admin/task-outbound-operation/*`` routes: identical request/response
shapes, but the caller must own the ``assistant_id`` referenced in the
payload (system/admin keys bypass the check via ``require_owned_assistant``).
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from orchestra.db.dependencies import get_db_session
from orchestra.services.task_machine_state_service import TASK_MACHINE_PROJECT_NAME
from orchestra.web.api.log.task_machine_admin import (
    create_or_adopt_task_outbound_operation_core,
    create_or_adopt_task_run_core,
    get_latest_task_run_core,
    patch_task_outbound_operation_core,
    patch_task_run_core,
)
from orchestra.web.api.log.task_machine_schema import (
    TaskOutboundOperationCreateOrAdoptRequest,
    TaskOutboundOperationMutationResponse,
    TaskOutboundOperationUpdateRequest,
    TaskRunCreateOrAdoptRequest,
    TaskRunLatestRequest,
    TaskRunLatestResponse,
    TaskRunMutationResponse,
    TaskRunUpdateRequest,
)
from orchestra.web.api.utils.assistant_ownership import require_owned_assistant

router = APIRouter()


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
