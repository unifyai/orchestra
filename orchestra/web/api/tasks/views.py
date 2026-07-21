import logging
import os
import uuid
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, Path, Response, status
from fastapi.routing import APIRouter
from starlette.requests import Request

from orchestra.db.dependencies import get_db_session, transient_request_db_session
from orchestra.provider_triggers.task_trigger import parse_task_trigger
from orchestra.services.task_cancel_service import (
    apply_task_cancel,
    resolve_task_cancel_target,
)
from orchestra.services.task_machine_state_service import (
    TASK_MACHINE_PROJECT_NAME,
    _requires_computer_from_row,
    _requires_filesystem_from_row,
)
from orchestra.services.task_mutation_contract import (
    TaskRevisionConflict,
    format_task_etag,
    parse_if_match,
)
from orchestra.services.task_mutation_service import TaskMutationService
from orchestra.services.task_trigger_service import (
    TaskTriggerNotRunnable,
    TaskTriggerTarget,
    resolve_task_trigger_target,
)
from orchestra.web.api.assistant.schema import InfoResponse
from orchestra.web.api.log.task_machine_admin import _get_internal_project_or_404
from orchestra.web.api.log.task_machine_schema import ProviderEventContextResponse
from orchestra.web.api.log.task_machine_user import (
    delete_owned_task_event_context,
    read_owned_task_event_context,
)
from orchestra.web.api.tasks.schema import (
    RetryTriggerResponse,
    StagedProviderTrigger,
    TaskCancelRequest,
    TaskCancelStatus,
    TaskRevisionConflictResponse,
    TaskTriggerRequest,
    TaskTriggerStatus,
    TriggerCatalogResponse,
    TriggerHealthResponse,
    TypedTaskCreateRequest,
    TypedTaskListResponse,
    TypedTaskPatchRequest,
    TypedTaskResponse,
)
from orchestra.web.api.utils.assistant_ownership import require_owned_assistant
from orchestra.web.api.utils.http_client import get_async_client

logger = logging.getLogger(__name__)

router = APIRouter()

ADAPTERS_URL = os.environ.get("UNITY_ADAPTERS_URL")
COMMS_URL = os.environ.get("UNITY_COMMS_URL")
ADMIN_KEY = os.environ.get("ORCHESTRA_ADMIN_KEY")


def _topology_remediation(unavailable_reason: str | None) -> str:
    """Return user-facing remediation copy for one topology failure."""

    messages = {
        "callback_url_unconfigured": (
            "Provider-trigger callbacks are not configured for this deployment."
        ),
        "callback_url_not_https": (
            "Provider-trigger callbacks require a public HTTPS callback base URL."
        ),
        "callback_url_internal": (
            "Provider-trigger callbacks require an externally reachable HTTPS URL."
        ),
        "event_storage_unconfigured": (
            "Provider-event storage is not configured for this deployment."
        ),
        "signing_secret_unconfigured": (
            "Provider webhook signing secrets are not configured for this deployment."
        ),
        "worker_unhealthy": (
            "The provider-trigger worker is not healthy for this deployment."
        ),
    }
    return messages.get(
        unavailable_reason or "",
        "Provider triggers are unavailable in this deployment.",
    )


@dataclass(frozen=True)
class _OwnedTaskEventContextScope:
    assistant_id: int
    project_id: int
    actor: str


def _owned_task_event_context_scope(
    request: Request,
    assistant_id: int,
    session,
    *,
    write: bool,
) -> _OwnedTaskEventContextScope:
    assistant = require_owned_assistant(request, assistant_id, session, write=write)
    project = _get_internal_project_or_404(
        session,
        project_name=TASK_MACHINE_PROJECT_NAME,
        assistant_id=str(assistant.agent_id),
    )
    actor = getattr(request.state, "user_id", None) or str(assistant.user_id)
    return _OwnedTaskEventContextScope(
        assistant_id=assistant.agent_id,
        project_id=project.id,
        actor=actor,
    )


async def _dispatch_task_trigger(target: TaskTriggerTarget) -> str:
    """Route one REST task trigger to the correct execution lane.

    Hosted offline tasks go straight to Communication's headless offline
    dispatch (no live assistant wake). Live hosted tasks and local offline
    tasks emit a ``task_trigger`` system event. Local live tasks keep the
    historical no-op skip (local runtime owns its own wake path).
    """

    request_id = uuid.uuid4().hex
    if target.offline and not target.is_local:
        await _dispatch_offline_task_to_comms(target=target, source_ref=request_id)
        return request_id
    if target.is_local and not target.offline:
        logger.info(
            "Skipping task-trigger dispatch for local assistant %s",
            target.assistant_id,
        )
        return request_id
    await _emit_task_trigger_system_event(
        assistant_id=target.assistant_id,
        task_id=target.task_id,
        source_task_log_id=target.source_task_log_id,
        task_label=target.task_name,
        task_summary=target.task_description,
        destination=target.destination,
        source_ref=request_id,
    )
    return request_id


async def _dispatch_offline_task_to_comms(
    *,
    target: TaskTriggerTarget,
    source_ref: str,
) -> None:
    """Launch one hosted offline task via Communication (admin-authenticated)."""

    if not target.revision:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Offline task {target.task_id} has no current execution revision; "
                "cannot dispatch headless execution."
            ),
        )
    comms_url = (COMMS_URL or "").rstrip("/")
    if not comms_url or not ADMIN_KEY:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "UNITY_COMMS_URL and ORCHESTRA_ADMIN_KEY are required to dispatch "
                "offline task triggers."
            ),
        )
    payload: dict = {
        "assistant_id": str(target.assistant_id),
        "task_id": target.task_id,
        "source_task_log_id": target.source_task_log_id,
        "revision": target.revision,
        "delivery": "offline",
        "wake": "explicit",
        "source_ref": source_ref,
        "source_medium": "api",
        "task_name": target.task_name,
        "task_description": target.task_description,
        "requires_filesystem": target.requires_filesystem,
        "requires_computer": target.requires_computer,
    }
    if target.destination:
        payload["destination"] = target.destination
    if target.entrypoint is not None:
        payload["entrypoint"] = target.entrypoint
    if target.max_runtime_seconds is not None:
        payload["max_runtime_seconds"] = target.max_runtime_seconds
    client = get_async_client()
    response = await client.post(
        f"{comms_url}/infra/task-execution/offline-dispatch",
        headers={
            "Authorization": f"Bearer {ADMIN_KEY}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=30,
    )
    if response.status_code >= 400:
        logger.error(
            "Offline task-trigger Comms dispatch failed: %s %s",
            response.status_code,
            response.text,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                "Communication offline-dispatch failed for task "
                f"{target.task_id}: HTTP {response.status_code}"
            ),
        )


async def _emit_task_trigger_system_event(
    *,
    assistant_id: int,
    task_id: int,
    source_task_log_id: int,
    task_label: str,
    task_summary: str,
    destination: str | None,
    source_ref: str,
) -> None:
    """Wake the assistant runtime with a ``task_trigger`` system event."""

    adapters_url = ADAPTERS_URL
    if not adapters_url:
        logger.warning("UNITY_ADAPTERS_URL not set, skipping task-trigger dispatch")
        return
    extra_event_fields: dict = {
        "type": "task_trigger",
        "task_id": task_id,
        "source_task_log_id": source_task_log_id,
        "source_ref": source_ref,
        "task_label": task_label,
        "task_summary": task_summary,
    }
    if destination:
        extra_event_fields["destination"] = destination
    client = get_async_client()
    response = await client.post(
        f"{adapters_url}/unity/system-event",
        headers={
            "Authorization": f"Bearer {ADMIN_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "assistant_id": assistant_id,
            "event_type": "task_trigger",
            "message": f"Task {task_id} triggered via REST API.",
            "extra_event_fields": extra_event_fields,
        },
        timeout=30,
    )
    if response.status_code != 200:
        logger.error(
            "Task-trigger adapter dispatch failed: %s %s",
            response.status_code,
            response.text,
        )


# Backward-compatible alias used by older tests / callers.
async def _dispatch_task_trigger_to_adapters(
    *,
    assistant_id: int,
    task_id: int,
    source_task_log_id: int,
    task_label: str,
    task_summary: str,
    is_local: bool,
) -> str:
    request_id = uuid.uuid4().hex
    if is_local:
        logger.info(
            "Skipping task-trigger dispatch for local assistant %s",
            assistant_id,
        )
        return request_id
    await _emit_task_trigger_system_event(
        assistant_id=assistant_id,
        task_id=task_id,
        source_task_log_id=source_task_log_id,
        task_label=task_label,
        task_summary=task_summary,
        destination=None,
        source_ref=request_id,
    )
    return request_id


def _typed_task_response(
    *,
    assistant_id: int,
    log_event_id: int,
    task_id: int,
    task_revision: int,
    data: dict,
) -> TypedTaskResponse:
    trigger = data.get("trigger")
    parsed_trigger = None
    if trigger is not None:
        try:
            parsed_trigger = parse_task_trigger(trigger)
        except (TypeError, ValueError):
            parsed_trigger = trigger
    return TypedTaskResponse(
        log_event_id=log_event_id,
        task_id=task_id,
        task_revision=task_revision,
        assistant_id=assistant_id,
        name=data.get("name"),
        description=data.get("description"),
        status=data.get("status"),
        enabled=data.get("enabled"),
        offline=data.get("offline"),
        requires_filesystem=_requires_filesystem_from_row(data),
        requires_computer=_requires_computer_from_row(data),
        trigger=parsed_trigger,
        schedule=data.get("schedule"),
        priority=data.get("priority"),
        entrypoint=data.get("entrypoint"),
        provider_event_binding_id=data.get("provider_event_binding_id"),
        raw=data,
    )


def _attach_task_etag(response: Response, task_revision: int) -> None:
    response.headers["ETag"] = format_task_etag(task_revision)


def _require_if_match(if_match: str | None) -> int:
    try:
        return parse_if_match(if_match)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_428_PRECONDITION_REQUIRED,
            detail="If-Match header with task_revision is required.",
        ) from exc


def _conflict_response(exc: TaskRevisionConflict) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=TaskRevisionConflictResponse(
            task_revision=exc.latest_revision,
        ).model_dump(),
    )


@router.get(
    "/assistants/{assistant_id}/tasks",
    response_model=InfoResponse[TypedTaskListResponse],
    tags=["Tasks"],
    summary="List assistant tasks",
)
def list_assistant_tasks(
    request: Request,
    assistant_id: int = Path(..., description="Assistant agent id."),
    session=Depends(get_db_session),
) -> InfoResponse[TypedTaskListResponse]:
    assistant = require_owned_assistant(request, assistant_id, session, write=False)
    service = TaskMutationService(session)
    rows = service.list_tasks(assistant=assistant)
    return InfoResponse(
        info=TypedTaskListResponse(
            tasks=[
                _typed_task_response(
                    assistant_id=assistant_id,
                    log_event_id=row.log_event_id,
                    task_id=row.task_id,
                    task_revision=row.task_revision,
                    data=row.data,
                )
                for row in rows
            ],
        ),
    )


@router.post(
    "/assistants/{assistant_id}/tasks",
    status_code=status.HTTP_201_CREATED,
    response_model=InfoResponse[TypedTaskResponse],
    tags=["Tasks"],
    summary="Create an assistant task",
)
def create_assistant_task(
    request: Request,
    body: TypedTaskCreateRequest,
    response: Response,
    assistant_id: int = Path(..., description="Assistant agent id."),
    session=Depends(get_db_session),
) -> InfoResponse[TypedTaskResponse]:
    assistant = require_owned_assistant(request, assistant_id, session, write=True)
    entries = body.model_dump(exclude_none=True)
    if body.trigger is not None:
        entries["trigger"] = (
            body.trigger.model_dump()
            if hasattr(body.trigger, "model_dump")
            else body.trigger
        )
    if body.status is None:
        entries["status"] = "triggerable" if entries.get("trigger") else "scheduled"
    service = TaskMutationService(session)
    try:
        result = service.create_task(assistant=assistant, entries=entries)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    session.commit()
    _attach_task_etag(response, result.task_revision)
    return InfoResponse(
        info=_typed_task_response(
            assistant_id=assistant_id,
            log_event_id=result.log_event_id,
            task_id=result.task_id,
            task_revision=result.task_revision,
            data=result.data,
        ),
    )


@router.get(
    "/assistants/{assistant_id}/tasks/{task_id}",
    response_model=InfoResponse[TypedTaskResponse],
    tags=["Tasks"],
    summary="Read one assistant task",
)
def get_assistant_task(
    request: Request,
    response: Response,
    assistant_id: int = Path(..., description="Assistant agent id."),
    task_id: int = Path(..., description="Logical task id."),
    session=Depends(get_db_session),
) -> InfoResponse[TypedTaskResponse]:
    assistant = require_owned_assistant(request, assistant_id, session, write=False)
    service = TaskMutationService(session)
    row = service.get_task(assistant=assistant, task_id=task_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task not found.",
        )
    _attach_task_etag(response, row.task_revision)
    return InfoResponse(
        info=_typed_task_response(
            assistant_id=assistant_id,
            log_event_id=row.log_event_id,
            task_id=row.task_id,
            task_revision=row.task_revision,
            data=row.data,
        ),
    )


@router.patch(
    "/assistants/{assistant_id}/tasks/{task_id}",
    response_model=InfoResponse[TypedTaskResponse],
    tags=["Tasks"],
    summary="Update one assistant task",
)
def patch_assistant_task(
    request: Request,
    body: TypedTaskPatchRequest,
    response: Response,
    assistant_id: int = Path(..., description="Assistant agent id."),
    task_id: int = Path(..., description="Logical task id."),
    if_match: str | None = Header(default=None, alias="If-Match"),
    session=Depends(get_db_session),
) -> InfoResponse[TypedTaskResponse]:
    assistant = require_owned_assistant(request, assistant_id, session, write=True)
    expected_revision = _require_if_match(if_match)
    updates = body.model_dump(exclude_none=True)
    if body.trigger is not None:
        updates["trigger"] = (
            body.trigger.model_dump()
            if hasattr(body.trigger, "model_dump")
            else body.trigger
        )
    service = TaskMutationService(session)
    try:
        result = service.mutate_authored_task(
            assistant=assistant,
            task_id=task_id,
            expected_task_revision=expected_revision,
            updates=updates,
        )
    except TaskRevisionConflict as exc:
        raise _conflict_response(exc) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    session.commit()
    _attach_task_etag(response, result.task_revision)
    return InfoResponse(
        info=_typed_task_response(
            assistant_id=assistant_id,
            log_event_id=result.log_event_id,
            task_id=result.task_id,
            task_revision=result.task_revision,
            data=result.data,
        ),
    )


@router.delete(
    "/assistants/{assistant_id}/tasks/{task_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["Tasks"],
    summary="Delete one assistant task",
)
def delete_assistant_task(
    request: Request,
    assistant_id: int = Path(..., description="Assistant agent id."),
    task_id: int = Path(..., description="Logical task id."),
    if_match: str | None = Header(default=None, alias="If-Match"),
    session=Depends(get_db_session),
) -> Response:
    assistant = require_owned_assistant(request, assistant_id, session, write=True)
    expected_revision = _require_if_match(if_match)
    service = TaskMutationService(session)
    try:
        service.delete_task(
            assistant=assistant,
            task_id=task_id,
            expected_task_revision=expected_revision,
        )
    except TaskRevisionConflict as exc:
        raise _conflict_response(exc) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/assistants/{assistant_id}/tasks/{task_id}/pause",
    response_model=InfoResponse[TypedTaskResponse],
    tags=["Tasks"],
    summary="Pause provider-event automation",
)
def pause_assistant_task_trigger(
    request: Request,
    response: Response,
    assistant_id: int = Path(..., description="Assistant agent id."),
    task_id: int = Path(..., description="Logical task id."),
    if_match: str | None = Header(default=None, alias="If-Match"),
    session=Depends(get_db_session),
) -> InfoResponse[TypedTaskResponse]:
    assistant = require_owned_assistant(request, assistant_id, session, write=True)
    expected_revision = _require_if_match(if_match)
    service = TaskMutationService(session)
    try:
        result = service.pause_provider_trigger(
            assistant=assistant,
            task_id=task_id,
            expected_task_revision=expected_revision,
        )
    except TaskRevisionConflict as exc:
        raise _conflict_response(exc) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    session.commit()
    _attach_task_etag(response, result.task_revision)
    return InfoResponse(
        info=_typed_task_response(
            assistant_id=assistant_id,
            log_event_id=result.log_event_id,
            task_id=result.task_id,
            task_revision=result.task_revision,
            data=result.data,
        ),
    )


@router.post(
    "/assistants/{assistant_id}/tasks/{task_id}/resume",
    response_model=InfoResponse[TypedTaskResponse],
    tags=["Tasks"],
    summary="Resume provider-event automation",
)
def resume_assistant_task_trigger(
    request: Request,
    response: Response,
    assistant_id: int = Path(..., description="Assistant agent id."),
    task_id: int = Path(..., description="Logical task id."),
    if_match: str | None = Header(default=None, alias="If-Match"),
    session=Depends(get_db_session),
) -> InfoResponse[TypedTaskResponse]:
    assistant = require_owned_assistant(request, assistant_id, session, write=True)
    expected_revision = _require_if_match(if_match)
    service = TaskMutationService(session)
    try:
        result = service.resume_provider_trigger(
            assistant=assistant,
            task_id=task_id,
            expected_task_revision=expected_revision,
        )
    except TaskRevisionConflict as exc:
        raise _conflict_response(exc) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    session.commit()
    _attach_task_etag(response, result.task_revision)
    return InfoResponse(
        info=_typed_task_response(
            assistant_id=assistant_id,
            log_event_id=result.log_event_id,
            task_id=result.task_id,
            task_revision=result.task_revision,
            data=result.data,
        ),
    )


@router.post(
    "/assistants/{assistant_id}/tasks/{task_id}/retry-trigger",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=InfoResponse[RetryTriggerResponse],
    tags=["Tasks"],
    summary="Request immediate trigger reconciliation",
)
def retry_assistant_task_trigger(
    request: Request,
    assistant_id: int = Path(..., description="Assistant agent id."),
    task_id: int = Path(..., description="Logical task id."),
    session=Depends(get_db_session),
) -> InfoResponse[RetryTriggerResponse]:
    assistant = require_owned_assistant(request, assistant_id, session, write=True)
    service = TaskMutationService(session)
    row = service.get_task(assistant=assistant, task_id=task_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task not found.",
        )
    try:
        service.retry_provider_trigger(assistant=assistant, task_id=task_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    return InfoResponse(info=RetryTriggerResponse(task_id=task_id))


@router.get(
    "/assistants/{assistant_id}/tasks/{task_id}/trigger-health",
    response_model=InfoResponse[TriggerHealthResponse],
    tags=["Tasks"],
    summary="Read composed provider-trigger health",
)
def get_assistant_task_trigger_health(
    request: Request,
    assistant_id: int = Path(..., description="Assistant agent id."),
    task_id: int = Path(..., description="Logical task id."),
    session=Depends(get_db_session),
) -> InfoResponse[TriggerHealthResponse]:
    assistant = require_owned_assistant(request, assistant_id, session, write=False)
    service = TaskMutationService(session)
    row = service.get_task(assistant=assistant, task_id=task_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task not found.",
        )
    trigger = parse_task_trigger(row.data.get("trigger"))
    authored_state = (
        trigger.state
        if trigger is not None and trigger.kind == "provider_event"
        else None
    )
    binding = service.get_binding_for_task(assistant=assistant, task_id=task_id)
    remediation = None
    runtime_health: str = "absent"
    desired_activation_revision = None
    observed_activation_revision = None
    acceptance_epoch = None
    local_acceptance_open = False
    active_generation_id = None
    coverage_started_at = None
    coverage_ended_at = None
    if binding is not None:
        runtime_health = binding.runtime_health
        desired_activation_revision = binding.desired_activation_revision
        observed_activation_revision = binding.observed_activation_revision
        acceptance_epoch = binding.acceptance_epoch
        local_acceptance_open = binding.local_acceptance_open
        active_generation_id = binding.active_generation_id
        if binding.coverage_started_at is not None:
            coverage_started_at = binding.coverage_started_at.isoformat()
        if binding.coverage_ended_at is not None:
            coverage_ended_at = binding.coverage_ended_at.isoformat()
        if runtime_health == "needs_attention":
            remediation = "Review the provider connection and trigger configuration."
        elif runtime_health == "provisioning":
            remediation = "Trigger provisioning is in progress."
    from orchestra.provider_triggers.topology import evaluate_provider_trigger_topology

    topology = evaluate_provider_trigger_topology(session)
    event_storage_configured = topology.event_storage_configured
    if (
        binding is not None
        and not topology.available
        and runtime_health not in {"absent", "removing"}
    ):
        runtime_health = "needs_attention"
        remediation = _topology_remediation(topology.unavailable_reason)
    return InfoResponse(
        info=TriggerHealthResponse(
            task_id=task_id,
            task_revision=row.task_revision,
            authored_trigger_state=authored_state,
            task_enabled=bool(row.data.get("enabled", True)),
            runtime_health=runtime_health,  # type: ignore[arg-type]
            desired_revision=desired_activation_revision,
            observed_revision=observed_activation_revision,
            acceptance_epoch=acceptance_epoch,
            local_acceptance_open=local_acceptance_open,
            active_generation_id=active_generation_id,
            coverage_started_at=coverage_started_at,
            coverage_ended_at=coverage_ended_at,
            remediation=remediation,
            event_storage_configured=event_storage_configured,
        ),
    )


@router.get(
    "/assistants/{assistant_id}/tasks/{task_id}/runs/{run_id}/event-context",
    response_model=InfoResponse[ProviderEventContextResponse],
    tags=["Tasks"],
    summary="Inspect one provider-event run context",
)
def get_assistant_task_run_event_context(
    request: Request,
    assistant_id: int = Path(..., description="Assistant agent id."),
    task_id: int = Path(..., description="Logical task id."),
    run_id: int = Path(..., description="Stable run identifier."),
    session=Depends(get_db_session),
) -> InfoResponse[ProviderEventContextResponse]:
    scope = _owned_task_event_context_scope(
        request,
        assistant_id,
        session,
        write=False,
    )
    return InfoResponse(
        info=read_owned_task_event_context(
            session,
            project_id=scope.project_id,
            assistant_id=scope.assistant_id,
            task_id=task_id,
            run_id=run_id,
            actor=scope.actor,
        ),
    )


@router.post(
    "/assistants/{assistant_id}/tasks/{task_id}/runs/{run_id}/event-context/export",
    response_model=InfoResponse[ProviderEventContextResponse],
    tags=["Tasks"],
    summary="Export one provider-event run context",
)
def export_assistant_task_run_event_context(
    request: Request,
    assistant_id: int = Path(..., description="Assistant agent id."),
    task_id: int = Path(..., description="Logical task id."),
    run_id: int = Path(..., description="Stable run identifier."),
    session=Depends(get_db_session),
) -> InfoResponse[ProviderEventContextResponse]:
    scope = _owned_task_event_context_scope(
        request,
        assistant_id,
        session,
        write=False,
    )
    return InfoResponse(
        info=read_owned_task_event_context(
            session,
            project_id=scope.project_id,
            assistant_id=scope.assistant_id,
            task_id=task_id,
            run_id=run_id,
            actor=scope.actor,
            export=True,
        ),
    )


@router.delete(
    "/assistants/{assistant_id}/tasks/{task_id}/runs/{run_id}/event-context",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["Tasks"],
    summary="Delete one provider-event run context",
)
def delete_assistant_task_run_event_context(
    request: Request,
    assistant_id: int = Path(..., description="Assistant agent id."),
    task_id: int = Path(..., description="Logical task id."),
    run_id: int = Path(..., description="Stable run identifier."),
    session=Depends(get_db_session),
) -> Response:
    scope = _owned_task_event_context_scope(
        request,
        assistant_id,
        session,
        write=True,
    )
    delete_owned_task_event_context(
        session,
        project_id=scope.project_id,
        assistant_id=scope.assistant_id,
        task_id=task_id,
        run_id=run_id,
        actor=scope.actor,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/assistants/{assistant_id}/provider-triggers",
    response_model=InfoResponse[TriggerCatalogResponse],
    tags=["Tasks"],
    summary="List provider triggers available for one assistant's connections",
)
def get_assistant_provider_triggers(
    assistant_id: int,
    backend_id: str | None = None,
    session=Depends(get_db_session),
) -> InfoResponse[TriggerCatalogResponse]:
    from orchestra.services.staged_trigger_catalog_service import (
        list_staged_triggers_for_assistant,
    )

    payload = list_staged_triggers_for_assistant(
        session,
        assistant_id=assistant_id,
        backend_id=backend_id,
    )
    return InfoResponse(
        info=TriggerCatalogResponse(
            available=payload["available"],
            unavailable_reason=payload["unavailable_reason"],
            triggers=[
                StagedProviderTrigger.model_validate(item)
                for item in payload["triggers"]
            ],
        ),
    )


@router.get(
    "/task-trigger-catalog",
    response_model=InfoResponse[TriggerCatalogResponse],
    tags=["Tasks"],
    summary="List provider triggers (requires assistant_id query param)",
    deprecated=True,
)
def get_task_trigger_catalog(
    assistant_id: int,
    backend_id: str | None = None,
    session=Depends(get_db_session),
) -> InfoResponse[TriggerCatalogResponse]:
    return get_assistant_provider_triggers(
        assistant_id=assistant_id,
        backend_id=backend_id,
        session=session,
    )


@router.post(
    "/tasks/{task_id}/trigger",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=InfoResponse[TaskTriggerStatus],
    tags=["Tasks"],
    summary="Trigger an assistant task",
    description=(
        "Trigger a task by logical task id for a specific assistant. The request "
        "body must include assistant_id. The task starts asynchronously in the "
        "assistant runtime when that assistant/task pair is accessible under the "
        "caller's API-key scope. Offline tasks are dispatched headlessly via "
        "Communication."
    ),
)
async def trigger_task(
    request: Request,
    body: TaskTriggerRequest,
    task_id: int = Path(
        ...,
        description="The logical task id to trigger.",
        example=123,
    ),
) -> InfoResponse[TaskTriggerStatus]:
    # Resolve under a short-lived session that is committed and closed before
    # outbound HTTP. Offline dispatch can re-enter Orchestra; holding the
    # request-scoped Depends session across that round-trip previously caused
    # field_type lock timeouts.
    with transient_request_db_session(request) as session:
        try:
            target = resolve_task_trigger_target(
                session,
                user_id=request.state.user_id,
                organization_id=getattr(request.state, "organization_id", None),
                task_id=task_id,
                assistant_id=body.assistant_id,
            )
        except TaskTriggerNotRunnable as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc
        if target is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Task not found.",
            )

    await _dispatch_task_trigger(target)
    return InfoResponse(
        info=TaskTriggerStatus(
            task_id=target.task_id,
            assistant_id=target.assistant_id,
            source_task_log_id=target.source_task_log_id,
        ),
    )


@router.post(
    "/tasks/{task_id}/cancel",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=InfoResponse[TaskCancelStatus],
    tags=["Tasks"],
    summary="Cancel an assistant task",
    description=(
        "Gracefully cancel a task by logical task id for a specific assistant. "
        "Marks the task definition cancelled, clears open Tasks/Executions "
        "rows, marks any inflight Tasks/Executions row cancelled, and stops the "
        "associated offline Kubernetes job when one is recorded. The request "
        "body must include assistant_id."
    ),
)
async def cancel_task(
    request: Request,
    body: TaskCancelRequest,
    task_id: int = Path(
        ...,
        description="The logical task id to cancel.",
        example=123,
    ),
) -> InfoResponse[TaskCancelStatus]:
    with transient_request_db_session(request) as session:
        target = resolve_task_cancel_target(
            session,
            user_id=request.state.user_id,
            organization_id=getattr(request.state, "organization_id", None),
            task_id=task_id,
            assistant_id=body.assistant_id,
        )
        if target is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Task not found.",
            )
        try:
            result = apply_task_cancel(
                session,
                user_id=request.state.user_id,
                organization_id=getattr(request.state, "organization_id", None),
                target=target,
                reason=body.reason,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc
        if result.already_terminal:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(f"Task {task_id} is already {result.task_status_before}."),
            )
        job_name = result.job_name
        cancel_target = result.target
        run_key = result.run_key

    job_stop_requested = False
    if job_name and not cancel_target.is_local:
        job_stop_requested = await _stop_comms_job(job_name=job_name)

    if not cancel_target.offline and not cancel_target.is_local:
        await _emit_task_cancel_system_event(
            assistant_id=cancel_target.assistant_id,
            task_id=cancel_target.task_id,
            source_task_log_id=cancel_target.source_task_log_id,
            destination=cancel_target.destination,
            reason=body.reason,
        )

    return InfoResponse(
        info=TaskCancelStatus(
            task_id=cancel_target.task_id,
            assistant_id=cancel_target.assistant_id,
            status="cancelled",
            run_key=run_key,
            job_name=job_name,
            job_stop_requested=job_stop_requested,
        ),
    )


async def _stop_comms_job(*, job_name: str) -> bool:
    """Ask Communication to suspend one Kubernetes job by name."""

    comms_url = (COMMS_URL or "").rstrip("/")
    if not comms_url or not ADMIN_KEY:
        logger.warning(
            "Skipping job stop for %s; UNITY_COMMS_URL / ORCHESTRA_ADMIN_KEY unset",
            job_name,
        )
        return False
    client = get_async_client()
    response = await client.post(
        f"{comms_url}/infra/job/stop",
        data={"job_name": job_name},
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        timeout=30,
    )
    if response.status_code >= 400:
        logger.error(
            "Task-cancel Comms job stop failed for %s: %s %s",
            job_name,
            response.status_code,
            response.text,
        )
        return False
    return True


async def _emit_task_cancel_system_event(
    *,
    assistant_id: int,
    task_id: int,
    source_task_log_id: int,
    destination: str | None,
    reason: str | None,
) -> None:
    """Notify a live assistant runtime that a task definition was cancelled."""

    adapters_url = ADAPTERS_URL
    if not adapters_url:
        logger.warning("UNITY_ADAPTERS_URL not set, skipping task-cancel dispatch")
        return
    extra_event_fields: dict = {
        "type": "task_cancel",
        "task_id": task_id,
        "source_task_log_id": source_task_log_id,
        "reason": reason or "Cancelled via REST API.",
    }
    if destination:
        extra_event_fields["destination"] = destination
    client = get_async_client()
    response = await client.post(
        f"{adapters_url}/unity/system-event",
        headers={
            "Authorization": f"Bearer {ADMIN_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "assistant_id": assistant_id,
            "event_type": "task_cancel",
            "message": f"Task {task_id} cancelled via REST API.",
            "extra_event_fields": extra_event_fields,
        },
        timeout=30,
    )
    if response.status_code != 200:
        logger.error(
            "Task-cancel adapter dispatch failed: %s %s",
            response.status_code,
            response.text,
        )
