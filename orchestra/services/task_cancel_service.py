"""Cancel assistant tasks: mark Tasks/Runs terminal and return infra job refs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import LogEvent
from orchestra.services.task_machine_state_service import (
    _coerce_bool,
    _replace_log_payload,
    get_latest_task_run_for_task,
    update_task_run,
)
from orchestra.services.task_trigger_service import (
    TaskTriggerTarget,
    _coerce_int,
    _destination_from_context_name,
    _offline_activation_for_task,
    _requires_computer_from_row,
    _requires_filesystem_from_row,
    _resolve_assistant_id,
    _task_project_for_owner,
    _task_rows_for_id,
)

_TERMINAL_TASK_STATUSES = frozenset({"cancelled", "completed", "failed"})
_INFLIGHT_RUN_STATES = frozenset({"pending", "running"})


@dataclass(frozen=True)
class TaskCancelResult:
    """Outcome of applying a cancel to one task instance."""

    target: TaskTriggerTarget
    task_status_before: str
    task_status_after: str
    run_key: str | None
    run_state_before: str | None
    run_state_after: str | None
    job_name: str | None
    already_terminal: bool = False


def resolve_task_cancel_target(
    session: Session,
    *,
    user_id: str,
    organization_id: int | None,
    task_id: int,
    assistant_id: int,
) -> TaskTriggerTarget | None:
    """Return the best cancel target for one assistant + logical task id.

    Prefers an ``active`` instance (in-flight work). Otherwise picks the newest
    non-terminal instance (scheduled/triggerable) so callers can disarm pending
    work. Returns ``None`` when no matching assistant/task rows exist.
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
    targets: list[TaskTriggerTarget] = []
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
        destination = _destination_from_context_name(context_name)
        offline = _coerce_bool(data.get("offline"))
        enabled = True if "enabled" not in data else _coerce_bool(data.get("enabled"))
        activation_revision = None
        entrypoint = None
        max_runtime_seconds = None
        requires_filesystem = _requires_filesystem_from_row(data)
        requires_computer = _requires_computer_from_row(data)
        if offline:
            activation_snapshot = _offline_activation_for_task(
                session=session,
                project_id=project.id,
                assistant_id=resolved_assistant_id,
                task_id=task_id,
                destination=destination,
            )
            if activation_snapshot is not None:
                activation_revision = activation_snapshot.revision
                entrypoint = activation_snapshot.entrypoint
                max_runtime_seconds = activation_snapshot.max_runtime_seconds
                requires_filesystem = activation_snapshot.requires_filesystem
                requires_computer = activation_snapshot.requires_computer
        targets.append(
            TaskTriggerTarget(
                assistant_id=resolved_assistant_id,
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
            ),
        )

    if not targets:
        return None
    return _select_cancel_target(targets)


def apply_task_cancel(
    session: Session,
    *,
    user_id: str,
    organization_id: int | None,
    target: TaskTriggerTarget,
    reason: str | None = None,
) -> TaskCancelResult:
    """Mark the Tasks instance (and any inflight Run) cancelled; return job_name."""

    project = _task_project_for_owner(
        session=session,
        user_id=user_id,
        organization_id=organization_id,
    )
    if project is None:
        raise ValueError("Assistants project not found for caller.")

    task_row = (
        session.query(LogEvent)
        .filter(
            LogEvent.project_id == project.id,
            LogEvent.id == int(target.source_task_log_id),
        )
        .one_or_none()
    )
    if task_row is None:
        raise ValueError(
            f"Task log_event_id={target.source_task_log_id} not found.",
        )

    payload = dict(task_row.data or {})
    status_before = str(payload.get("status") or "")
    if status_before in _TERMINAL_TASK_STATUSES:
        return TaskCancelResult(
            target=target,
            task_status_before=status_before,
            task_status_after=status_before,
            run_key=None,
            run_state_before=None,
            run_state_after=None,
            job_name=None,
            already_terminal=True,
        )

    cancel_reason = (reason or "").strip() or "Cancelled via REST API."
    payload["status"] = "cancelled"
    info = payload.get("info")
    if isinstance(info, dict):
        info = dict(info)
        info["cancel_reason"] = cancel_reason
        payload["info"] = info
    else:
        payload["info"] = {"cancel_reason": cancel_reason}
    _replace_log_payload(task_row, payload)

    run_key: str | None = None
    run_state_before: str | None = None
    run_state_after: str | None = None
    job_name: str | None = None

    latest_run = get_latest_task_run_for_task(
        session,
        project.id,
        assistant_id=str(target.assistant_id),
        task_id=int(target.task_id),
        source_task_log_id=int(target.source_task_log_id),
    )
    if latest_run is not None and isinstance(latest_run.data, dict):
        run_data = dict(latest_run.data)
        run_key = str(run_data.get("run_key") or "") or None
        run_state_before = str(run_data.get("state") or "") or None
        job_name = str(run_data.get("job_name") or "") or None
        if run_state_before in _INFLIGHT_RUN_STATES and run_key:
            completed_at = datetime.now(timezone.utc).isoformat()
            update_task_run(
                session,
                project.id,
                assistant_id=str(target.assistant_id),
                run_key=run_key,
                updates={
                    "state": "cancelled",
                    "completed_at": completed_at,
                    "result_summary": cancel_reason,
                    "error": None,
                },
                source_task_log_id=int(target.source_task_log_id),
            )
            run_state_after = "cancelled"
        else:
            run_state_after = run_state_before

    session.flush()
    return TaskCancelResult(
        target=target,
        task_status_before=status_before,
        task_status_after="cancelled",
        run_key=run_key,
        run_state_before=run_state_before,
        run_state_after=run_state_after,
        job_name=job_name,
        already_terminal=False,
    )


def _select_cancel_target(targets: list[TaskTriggerTarget]) -> TaskTriggerTarget:
    """Prefer active instances, then other non-terminal, newest instance first."""

    return sorted(
        targets,
        key=lambda target: (
            _cancel_status_rank(target.status),
            0 if target.destination else 1,
            -target.instance_id,
            -target.source_task_log_id,
        ),
    )[0]


def _cancel_status_rank(status: str) -> int:
    normalized = (status or "").strip().lower()
    if normalized == "active":
        return 0
    if normalized in _TERMINAL_TASK_STATUSES:
        return 2
    return 1
