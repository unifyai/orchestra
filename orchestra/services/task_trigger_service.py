"""Resolve public task-trigger requests to assistant-owned task definition rows."""

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
    _coerce_bool,
    _coerce_int,
    _requires_computer_from_row,
    _requires_filesystem_from_row,
    get_open_task_execution,
    is_task_surface_context_name,
)

_TERMINAL_OR_UNRUNNABLE_STATUSES = {
    "active",
    "cancelled",
    "completed",
    "failed",
}


class TaskTriggerNotRunnable(ValueError):
    """Raised when the task definition cannot be explicitly triggered."""

    def __init__(self, *, task_id: int, status: str) -> None:
        self.task_id = task_id
        self.status = status
        super().__init__(
            f"Task {task_id} is not runnable (status={status!r}).",
        )


@dataclass(frozen=True)
class TaskTriggerTarget:
    assistant_id: int
    task_id: int
    source_task_log_id: int
    destination: str | None
    task_name: str
    task_description: str
    status: str
    is_local: bool
    offline: bool = False
    enabled: bool = True
    revision: str | None = None
    entrypoint: int | None = None
    max_runtime_seconds: int | None = None
    requires_filesystem: bool = False
    requires_computer: bool = False


def resolve_task_trigger_target(
    session: Session,
    *,
    user_id: str,
    organization_id: int | None,
    task_id: int,
    assistant_id: int,
) -> TaskTriggerTarget | None:
    """Return the accessible task definition for one assistant + logical task id."""

    project = _task_project_for_owner(
        session=session,
        user_id=user_id,
        organization_id=organization_id,
    )
    if project is None:
        return None

    requested_assistant_id = int(assistant_id)
    rows = _task_rows_for_id(
        session=session,
        project_id=project.id,
        task_id=task_id,
    )
    bound_rows: list[tuple[LogEvent, str, Assistant, dict[str, Any]]] = []
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
        if resolved_assistant_id != requested_assistant_id:
            continue
        bound_rows.append((row, context_name, assistant, data))

    if not bound_rows:
        return None

    targets = [
        _build_target_from_row(
            session=session,
            project_id=project.id,
            task_id=task_id,
            row=row,
            context_name=context_name,
            assistant=assistant,
            data=data,
        )
        for row, context_name, assistant, data in bound_rows
    ]
    runnable = [
        target
        for target in targets
        if target.status not in _TERMINAL_OR_UNRUNNABLE_STATUSES
    ]
    if not runnable:
        status = targets[0].status if targets else ""
        raise TaskTriggerNotRunnable(task_id=task_id, status=status)
    return _select_current_target(runnable)


def _build_target_from_row(
    *,
    session: Session,
    project_id: int,
    task_id: int,
    row: LogEvent,
    context_name: str,
    assistant: Assistant,
    data: dict[str, Any],
) -> TaskTriggerTarget:
    destination = _destination_from_context_name(context_name)
    offline = _coerce_bool(data.get("offline"))
    enabled = True if "enabled" not in data else _coerce_bool(data.get("enabled"))
    revision = None
    entrypoint = _coerce_int(data.get("entrypoint"))
    max_runtime_seconds = _coerce_int(data.get("max_runtime_seconds"))
    requires_filesystem = _requires_filesystem_from_row(data)
    requires_computer = _requires_computer_from_row(data)
    if offline:
        activation_snapshot = _offline_execution_for_task(
            session=session,
            project_id=project_id,
            assistant_id=int(assistant.agent_id),
            task_id=task_id,
            destination=destination,
        )
        if activation_snapshot is not None:
            revision = activation_snapshot.revision
            if activation_snapshot.entrypoint is not None:
                entrypoint = activation_snapshot.entrypoint
            if activation_snapshot.max_runtime_seconds is not None:
                max_runtime_seconds = activation_snapshot.max_runtime_seconds
            requires_filesystem = activation_snapshot.requires_filesystem
            requires_computer = activation_snapshot.requires_computer
    return TaskTriggerTarget(
        assistant_id=int(assistant.agent_id),
        task_id=task_id,
        source_task_log_id=int(row.id),
        destination=destination,
        task_name=str(data.get("name") or f"task {task_id}"),
        task_description=str(data.get("description") or ""),
        status=str(data.get("status") or ""),
        is_local=bool(assistant.is_local),
        offline=offline,
        enabled=enabled,
        revision=revision,
        entrypoint=entrypoint,
        max_runtime_seconds=max_runtime_seconds,
        requires_filesystem=requires_filesystem,
        requires_computer=requires_computer,
    )


@dataclass(frozen=True)
class _OfflineExecutionSnapshot:
    revision: str
    entrypoint: int | None
    max_runtime_seconds: int | None
    requires_filesystem: bool = False
    requires_computer: bool = False


def _offline_execution_for_task(
    *,
    session: Session,
    project_id: int,
    assistant_id: int,
    task_id: int,
    destination: str | None,
) -> _OfflineExecutionSnapshot | None:
    """Return revision + entrypoint for one open offline Execution, if present."""

    execution = get_open_task_execution(
        session,
        project_id,
        assistant_id=str(assistant_id),
        task_id=task_id,
        destination=destination,
    )
    if execution is None or not isinstance(execution.data, dict):
        return None
    revision = execution.data.get("revision")
    if revision in (None, ""):
        return None
    return _OfflineExecutionSnapshot(
        revision=str(revision),
        entrypoint=_coerce_int(execution.data.get("entrypoint")),
        max_runtime_seconds=_coerce_int(
            execution.data.get("max_runtime_seconds"),
        ),
        requires_filesystem=_requires_filesystem_from_row(execution.data),
        requires_computer=_requires_computer_from_row(execution.data),
    )


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
    rows = (
        session.query(LogEvent, Context.name)
        .join(LogEventContext, LogEventContext.log_event_id == LogEvent.id)
        .join(Context, Context.id == LogEventContext.context_id)
        .filter(
            LogEvent.project_id == project_id,
            LogEventContext.project_id == project_id,
            Context.project_id == project_id,
            LogEvent.data.has_key("task_id"),
            LogEvent.data.op("->>")("task_id") == str(task_id),
        )
        .all()
    )
    targets: list[tuple[LogEvent, str, Assistant]] = []
    for row, context_name in rows:
        if not is_task_surface_context_name(context_name):
            continue
        data = row.data if isinstance(row.data, dict) else {}
        resolved_assistant_id = _resolve_assistant_id(
            data=data,
            context_name=context_name,
        )
        if resolved_assistant_id is None:
            continue
        assistant = (
            session.query(Assistant)
            .filter(Assistant.agent_id == resolved_assistant_id)
            .one_or_none()
        )
        if assistant is None:
            continue
        targets.append((row, context_name, assistant))
    return targets


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
        segments = [
            segment for segment in context_name.strip("/").split("/") if segment
        ]
        if len(segments) == 3 and segments[0] == "Teams":
            return f"team:{segments[1]}"
    return None


def _select_current_target(targets: list[TaskTriggerTarget]) -> TaskTriggerTarget:
    """Prefer runnable, enabled, team-destined rows that can actually dispatch."""

    return sorted(
        targets,
        key=lambda target: (
            _row_status_rank(target),
            0 if target.enabled else 1,
            0 if target.destination else 1,
            0 if target.revision else 1,
            -target.source_task_log_id,
        ),
    )[0]


def _row_status_rank(target: TaskTriggerTarget) -> int:
    return 1 if target.status in _TERMINAL_OR_UNRUNNABLE_STATUSES else 0
