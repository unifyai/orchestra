import logging
import os
import uuid

from fastapi import Depends, HTTPException, Path, status
from fastapi.routing import APIRouter
from sqlalchemy.orm import Session
from starlette.requests import Request

from orchestra.db.dependencies import get_db_session
from orchestra.services.task_trigger_service import (
    TaskTriggerTarget,
    resolve_task_trigger_target,
)
from orchestra.web.api.assistant.schema import InfoResponse
from orchestra.web.api.tasks.schema import TaskTriggerRequest, TaskTriggerStatus
from orchestra.web.api.utils.http_client import get_async_client

logger = logging.getLogger(__name__)

router = APIRouter()

ADAPTERS_URL = os.environ.get("UNITY_ADAPTERS_URL")
COMMS_URL = os.environ.get("UNITY_COMMS_URL")
ADMIN_KEY = os.environ.get("ORCHESTRA_ADMIN_KEY")


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

    if not target.activation_revision:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Offline task {target.task_id} has no current activation revision; "
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
        "activation_revision": target.activation_revision,
        "execution_mode": "offline",
        "source_type": "explicit",
        "source_ref": source_ref,
        "source_medium": "api",
        "task_name": target.task_name,
        "task_description": target.task_description,
    }
    if target.destination:
        payload["destination"] = target.destination
    client = get_async_client()
    response = await client.post(
        f"{comms_url}/infra/task-activation/offline-dispatch",
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
    session: Session = Depends(get_db_session),
) -> InfoResponse[TaskTriggerStatus]:
    target = resolve_task_trigger_target(
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

    # Release any resolve-time DB locks before outbound HTTP. Offline dispatch
    # re-enters Orchestra via /admin/task-activation/current; holding this
    # request transaction open across that round-trip causes field_type lock
    # timeouts under concurrent ensure/upsert paths.
    session.commit()

    await _dispatch_task_trigger(target)
    return InfoResponse(
        info=TaskTriggerStatus(
            task_id=target.task_id,
            assistant_id=target.assistant_id,
        ),
    )
