"""Resolve public task-trigger requests to assistant-owned task rows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import (
    Assistant,
    Context,
    LogEvent,
    LogEventContext,
    Project,
)
from orchestra.services.task_machine_state_service import (
    TASK_MACHINE_PROJECT_NAME,
    TASKS_CONTEXT_NAME,
    is_task_surface_context_name,
)

_TERMINAL_OR_UNRUNNABLE_STATUSES = {
    "active",
    "cancelled",
    "completed",
    "failed",
}


@dataclass(frozen=True)
class TaskTriggerTarget:
    assistant_id: int
    task_id: int
    source_task_log_id: int
    destination: str | None
    task_name: str
    task_description: str
    status: str
    instance_id: int
    is_local: bool


class AmbiguousTaskTriggerTargetError(ValueError):
    """Raised when a task id maps to multiple assistants in the caller scope."""


def resolve_task_trigger_target(
    session: Session,
    *,
    user_id: str,
    organization_id: int | None,
    task_id: int,
) -> TaskTriggerTarget | None:
    """Return the unique accessible task target for a public task trigger."""

    project = _task_project_for_owner(
        session=session,
        user_id=user_id,
        organization_id=organization_id,
    )
    if project is None:
        return None

    rows = _task_rows_for_id(
        session=session,
        project_id=project.id,
        task_id=task_id,
    )
    targets_by_assistant: dict[int, list[TaskTriggerTarget]] = {}
    for row, context_name, assistant in rows:
        data = row.data if isinstance(row.data, dict) else {}
        resolved_assistant_id = _resolve_assistant_id(
            data=data,
            context_name=context_name,
        )
        if resolved_assistant_id is None:
            continue
        if int(assistant.agent_id) != resolved_assistant_id:
            continue
        target = TaskTriggerTarget(
            assistant_id=resolved_assistant_id,
            task_id=task_id,
            source_task_log_id=int(row.id),
            destination=_destination_from_context_name(context_name),
            task_name=str(data.get("name") or f"task {task_id}"),
            task_description=str(data.get("description") or ""),
            status=str(data.get("status") or ""),
            instance_id=_coerce_int(data.get("instance_id")) or 0,
            is_local=bool(assistant.is_local),
        )
        targets_by_assistant.setdefault(resolved_assistant_id, []).append(target)

    if not targets_by_assistant:
        return None
    if len(targets_by_assistant) > 1:
        raise AmbiguousTaskTriggerTargetError(
            f"Task id {task_id} is visible on multiple assistants.",
        )
    return _select_current_target(next(iter(targets_by_assistant.values())))


def _task_project_for_owner(
    *,
    session: Session,
    user_id: str,
    organization_id: int | None,
) -> Project | None:
    query = session.query(Project).filter(Project.name == TASK_MACHINE_PROJECT_NAME)
    if organization_id is None:
        query = query.filter(
            Project.user_id == user_id,
            Project.organization_id.is_(None),
        )
    else:
        query = query.filter(Project.organization_id == organization_id)
    return query.one_or_none()


def _task_rows_for_id(
    *,
    session: Session,
    project_id: int,
    task_id: int,
) -> list[tuple[LogEvent, str, Assistant]]:
    return (
        session.query(LogEvent, Context.name, Assistant)
        .join(LogEventContext, LogEventContext.log_event_id == LogEvent.id)
        .join(Context, Context.id == LogEventContext.context_id)
        .join(Assistant, Assistant.agent_id == Context.owner_id)
        .filter(
            LogEvent.project_id == project_id,
            LogEventContext.project_id == project_id,
            Context.project_id == project_id,
            Context.owner_scope == "assistant",
            Context.name.like(f"%/{TASKS_CONTEXT_NAME}"),
            LogEvent.data.has_key("task_id"),
            LogEvent.data.op("->>")("task_id") == str(task_id),
        )
        .all()
    )


def _resolve_assistant_id(
    *,
    data: dict[str, Any],
    context_name: str,
) -> int | None:
    for key in ("assistant_id", "_assistant_id"):
        value = data.get(key)
        if value not in (None, ""):
            return _coerce_int(value)
    segments = [segment for segment in context_name.strip("/").split("/") if segment]
    if len(segments) >= 2 and segments[-1] == TASKS_CONTEXT_NAME:
        return _coerce_int(segments[-2])
    return None


def _destination_from_context_name(context_name: str) -> str | None:
    if is_task_surface_context_name(context_name):
        segments = [segment for segment in context_name.strip("/").split("/") if segment]
        if len(segments) == 3 and segments[0] == "Teams":
            return f"team:{segments[1]}"
    return None


def _select_current_target(targets: list[TaskTriggerTarget]) -> TaskTriggerTarget:
    return sorted(
        targets,
        key=lambda target: (
            _row_status_rank(target),
            target.instance_id,
            target.source_task_log_id,
        ),
    )[0]


def _row_status_rank(target: TaskTriggerTarget) -> int:
    return 1 if target.status in _TERMINAL_OR_UNRUNNABLE_STATUSES else 0


def _coerce_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
