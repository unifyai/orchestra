import logging
import os
import uuid

from fastapi import Depends, HTTPException, Path, status
from fastapi.routing import APIRouter
from sqlalchemy.orm import Session
from starlette.requests import Request

from orchestra.db.dependencies import get_db_session
from orchestra.services.task_trigger_service import (
    AmbiguousTaskTriggerTargetError,
    resolve_task_trigger_target,
)
from orchestra.web.api.assistant.schema import InfoResponse
from orchestra.web.api.tasks.schema import TaskTriggerStatus
from orchestra.web.api.utils.http_client import get_async_client

logger = logging.getLogger(__name__)

router = APIRouter()

ADAPTERS_URL = os.environ.get("DROID_ADAPTERS_URL")
ADMIN_KEY = os.environ.get("ORCHESTRA_ADMIN_KEY")


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
        logger.info("Skipping task-trigger dispatch for local assistant %s", assistant_id)
        return request_id
    adapters_url = ADAPTERS_URL
    if not adapters_url:
        logger.warning("DROID_ADAPTERS_URL not set, skipping task-trigger dispatch")
        return request_id
    client = get_async_client()
    response = await client.post(
        f"{adapters_url}/droid/system-event",
        headers={
            "Authorization": f"Bearer {ADMIN_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "assistant_id": assistant_id,
            "event_type": "task_trigger",
            "message": f"Task {task_id} triggered via REST API.",
            "extra_event_fields": {
                "type": "task_trigger",
                "task_id": task_id,
                "source_task_log_id": source_task_log_id,
                "source_ref": request_id,
                "task_label": task_label,
                "task_summary": task_summary,
            },
        },
        timeout=30,
    )
    if response.status_code != 200:
        logger.error(
            "Task-trigger adapter dispatch failed: %s %s",
            response.status_code,
            response.text,
        )
    return request_id


@router.post(
    "/tasks/{task_id}/trigger",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=InfoResponse[TaskTriggerStatus],
    tags=["Tasks"],
    summary="Trigger an assistant task",
    description=(
        "Trigger a task by logical task id. The task starts asynchronously in "
        "the assistant runtime when the id resolves to exactly one accessible task."
    ),
)
async def trigger_task(
    request: Request,
    task_id: int = Path(
        ...,
        description="The logical task id to trigger.",
        example=123,
    ),
    session: Session = Depends(get_db_session),
) -> InfoResponse[TaskTriggerStatus]:
    try:
        target = resolve_task_trigger_target(
            session,
            user_id=request.state.user_id,
            organization_id=getattr(request.state, "organization_id", None),
            task_id=task_id,
        )
    except AmbiguousTaskTriggerTargetError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task not found.",
        )

    await _dispatch_task_trigger_to_adapters(
        assistant_id=target.assistant_id,
        task_id=target.task_id,
        source_task_log_id=target.source_task_log_id,
        task_label=target.task_name,
        task_summary=target.task_description,
        is_local=target.is_local,
    )
    return InfoResponse(
        info=TaskTriggerStatus(
            task_id=target.task_id,
            assistant_id=target.assistant_id,
        ),
    )
