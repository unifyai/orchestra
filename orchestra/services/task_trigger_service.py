"""Resolve public task-trigger requests to assistant-owned task rows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import (
    Assistant,
    Context,
    LogEvent,
    LogEventContext,
    Project,
)
from orchestra.db.scope import owner_key_for_context
from orchestra.services.task_machine_state_service import (
    TASK_MACHINE_PROJECT_NAME,
    TASKS_CONTEXT_NAME,
    _coerce_bool,
    _extract_key_order,
    _requires_computer_from_row,
    _requires_filesystem_from_row,
    get_task_activation,
    is_task_surface_context_name,
)

_TERMINAL_OR_UNRUNNABLE_STATUSES = {
    "active",
    "cancelled",
    "completed",
    "failed",
}

# Fields that must not be copied onto an explicit-trigger fork. Schedule/repeat
# are stripped so execute() does not re-arm the recurring weekly (or other)
# series from this one-off instance.
_FORK_EXCLUDED_FIELDS = frozenset(
    {
        "instance_id",
        "schedule",
        "repeat",
        "trigger",
        "activated_by",
        "status",
        "info",
        "provider_event_binding_id",
        "provider_event_launch_identity",
    },
)


class TaskTriggerInstanceNotRunnable(ValueError):
    """Raised when a caller-requested instance_id cannot be executed."""

    def __init__(self, *, task_id: int, instance_id: int, status: str) -> None:
        self.task_id = task_id
        self.instance_id = instance_id
        self.status = status
        super().__init__(
            f"Task {task_id} instance {instance_id} is not runnable "
            f"(status={status!r}).",
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
    instance_id: int
    is_local: bool
    offline: bool = False
    enabled: bool = True
    activation_revision: str | None = None
    entrypoint: int | None = None
    max_runtime_seconds: int | None = None
    requires_filesystem: bool = False
    requires_computer: bool = False
    forked: bool = False


def resolve_task_trigger_target(
    session: Session,
    *,
    user_id: str,
    organization_id: int | None,
    task_id: int,
    assistant_id: int,
    instance_id: int | None = None,
) -> TaskTriggerTarget | None:
    """Return the accessible task target for one assistant + logical task id.

    By default (``instance_id is None``) forks a brand-new Tasks instance that is
    decoupled from recurrence (no ``schedule`` / ``repeat``), then targets that
    row. When ``instance_id`` is set, dispatches that existing instance early.
    """

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

    if instance_id is not None:
        return _target_for_existing_instance(
            session=session,
            project_id=project.id,
            task_id=task_id,
            instance_id=int(instance_id),
            bound_rows=bound_rows,
        )

    return _fork_and_target_new_instance(
        session=session,
        project_id=project.id,
        task_id=task_id,
        bound_rows=bound_rows,
    )


def _target_for_existing_instance(
    *,
    session: Session,
    project_id: int,
    task_id: int,
    instance_id: int,
    bound_rows: list[tuple[LogEvent, str, Assistant, dict[str, Any]]],
) -> TaskTriggerTarget | None:
    """Resolve a caller-selected instance, refusing terminal/active rows."""

    matches = [
        item
        for item in bound_rows
        if (_coerce_int(item[3].get("instance_id")) or 0) == instance_id
    ]
    if not matches:
        return None
    targets = [
        _build_target_from_row(
            session=session,
            project_id=project_id,
            task_id=task_id,
            row=row,
            context_name=context_name,
            assistant=assistant,
            data=data,
            forked=False,
        )
        for row, context_name, assistant, data in matches
    ]
    runnable = [
        target
        for target in targets
        if target.status not in _TERMINAL_OR_UNRUNNABLE_STATUSES
    ]
    if not runnable:
        status = targets[0].status if targets else ""
        raise TaskTriggerInstanceNotRunnable(
            task_id=task_id,
            instance_id=instance_id,
            status=status,
        )
    return _select_current_target(runnable)


def _fork_and_target_new_instance(
    *,
    session: Session,
    project_id: int,
    task_id: int,
    bound_rows: list[tuple[LogEvent, str, Assistant, dict[str, Any]]],
) -> TaskTriggerTarget:
    """Insert a recurrence-decoupled Tasks instance and target it."""

    template_targets = [
        _build_target_from_row(
            session=session,
            project_id=project_id,
            task_id=task_id,
            row=row,
            context_name=context_name,
            assistant=assistant,
            data=data,
            forked=False,
        )
        for row, context_name, assistant, data in bound_rows
    ]
    template_target = _select_current_target(template_targets)
    template_row, context_name, assistant, template_data = next(
        item
        for item in bound_rows
        if int(item[0].id) == template_target.source_task_log_id
    )

    context = (
        session.query(Context)
        .filter(
            Context.project_id == project_id,
            Context.name == context_name,
        )
        .one()
    )
    next_instance_id = (
        max((_coerce_int(data.get("instance_id")) or 0) for _, _, _, data in bound_rows)
        + 1
    )
    fork_data = {
        key: value
        for key, value in template_data.items()
        if key not in _FORK_EXCLUDED_FIELDS
    }
    fork_data["task_id"] = task_id
    fork_data["instance_id"] = next_instance_id
    fork_data["status"] = "scheduled"
    fork_data["info"] = (
        "Forked for explicit REST trigger; decoupled from recurrence "
        "(no schedule/repeat)."
    )
    if "assistant_id" not in fork_data and "_assistant_id" not in fork_data:
        fork_data["assistant_id"] = str(assistant.agent_id)

    now = datetime.now(timezone.utc)
    owner_key = owner_key_for_context(session, context.id)
    fork_row = LogEvent(
        project_id=project_id,
        data=fork_data,
        key_order=_extract_key_order(fork_data),
        created_at=now,
        updated_at=now,
        owner_key=owner_key,
    )
    session.add(fork_row)
    session.flush()
    session.add(
        LogEventContext(
            project_id=project_id,
            log_event_id=fork_row.id,
            context_id=context.id,
            owner_key=owner_key,
        ),
    )
    session.flush()

    return _build_target_from_row(
        session=session,
        project_id=project_id,
        task_id=task_id,
        row=fork_row,
        context_name=context_name,
        assistant=assistant,
        data=fork_data,
        forked=True,
    )


def _build_target_from_row(
    *,
    session: Session,
    project_id: int,
    task_id: int,
    row: LogEvent,
    context_name: str,
    assistant: Assistant,
    data: dict[str, Any],
    forked: bool,
) -> TaskTriggerTarget:
    destination = _destination_from_context_name(context_name)
    offline = _coerce_bool(data.get("offline"))
    enabled = True if "enabled" not in data else _coerce_bool(data.get("enabled"))
    activation_revision = None
    entrypoint = _coerce_int(data.get("entrypoint"))
    max_runtime_seconds = _coerce_int(data.get("max_runtime_seconds"))
    requires_filesystem = _requires_filesystem_from_row(data)
    requires_computer = _requires_computer_from_row(data)
    if offline:
        activation_snapshot = _offline_activation_for_task(
            session=session,
            project_id=project_id,
            assistant_id=int(assistant.agent_id),
            task_id=task_id,
            destination=destination,
        )
        if activation_snapshot is not None:
            activation_revision = activation_snapshot.revision
            # Prefer the armed activation entrypoint when present so forks of
            # offline tasks stay aligned with the live symbolic contract.
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
        instance_id=_coerce_int(data.get("instance_id")) or 0,
        is_local=bool(assistant.is_local),
        offline=offline,
        enabled=enabled,
        activation_revision=activation_revision,
        entrypoint=entrypoint,
        max_runtime_seconds=max_runtime_seconds,
        requires_filesystem=requires_filesystem,
        requires_computer=requires_computer,
        forked=forked,
    )


@dataclass(frozen=True)
class _OfflineActivationSnapshot:
    revision: str
    entrypoint: int | None
    max_runtime_seconds: int | None
    requires_filesystem: bool = False
    requires_computer: bool = False


def _offline_activation_for_task(
    *,
    session: Session,
    project_id: int,
    assistant_id: int,
    task_id: int,
    destination: str | None,
) -> _OfflineActivationSnapshot | None:
    """Return revision + entrypoint for one offline task activation, if present."""

    activation = get_task_activation(
        session,
        project_id,
        assistant_id=str(assistant_id),
        task_id=task_id,
        destination=destination,
    )
    if activation is None or not isinstance(activation.data, dict):
        return None
    revision = activation.data.get("activation_revision")
    if revision in (None, ""):
        return None
    return _OfflineActivationSnapshot(
        revision=str(revision),
        entrypoint=_coerce_int(activation.data.get("entrypoint")),
        max_runtime_seconds=_coerce_int(
            activation.data.get("max_runtime_seconds"),
        ),
        requires_filesystem=_requires_filesystem_from_row(activation.data),
        requires_computer=_requires_computer_from_row(activation.data),
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
            Context.name.like(f"%/{TASKS_CONTEXT_NAME}"),
            LogEvent.data.has_key("task_id"),
            LogEvent.data.op("->>")("task_id") == str(task_id),
        )
        .all()
    )
    targets: list[tuple[LogEvent, str, Assistant]] = []
    for row, context_name in rows:
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
            0 if target.activation_revision else 1,
            -target.instance_id,
            -target.source_task_log_id,
        ),
    )[0]


def _row_status_rank(target: TaskTriggerTarget) -> int:
    return 1 if target.status in _TERMINAL_OR_UNRUNNABLE_STATUSES else 0


def _coerce_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
