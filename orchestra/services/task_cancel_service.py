"""Cancel assistant tasks: mark definition terminal and return infra job refs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import LogEvent
from orchestra.services.task_machine_state_service import (
    _delete_open_executions_for_task,
    _replace_log_payload,
    get_latest_task_execution_for_task,
    lookup_task_machine_executions_context_id,
    resolve_tasks_context_name,
    update_task_run,
)
from orchestra.services.task_trigger_service import (
    TaskTriggerTarget,
    _build_target_from_row,
    _resolve_assistant_id,
    _task_project_for_owner,
    _task_rows_for_id,
)

_TERMINAL_TASK_STATUSES = frozenset({"cancelled", "completed", "failed"})
_INFLIGHT_RUN_STATES = frozenset({"pending", "running"})


@dataclass(frozen=True)
class TaskCancelResult:
    """Outcome of applying a cancel to one task definition."""

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
    """Return the task definition to cancel for one assistant + logical task id."""

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
        targets.append(
            _build_target_from_row(
                task_id=task_id,
                row=row,
                context_name=context_name,
                assistant=assistant,
                data=data,
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
    """Mark the task definition cancelled, clear open Executions, cancel inflight Runs."""

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

    tasks_context_name = resolve_tasks_context_name(
        session=session,
        project_id=project.id,
        assistant_id=str(target.assistant_id),
    )
    executions_context_id = lookup_task_machine_executions_context_id(
        session=session,
        project_id=project.id,
        tasks_context_name=tasks_context_name,
    )
    if executions_context_id is not None:
        _delete_open_executions_for_task(
            session,
            project_id=project.id,
            context_id=executions_context_id,
            task_id=int(target.task_id),
            destination=target.destination,
        )

    run_key: str | None = None
    run_state_before: str | None = None
    run_state_after: str | None = None
    job_name: str | None = None

    latest_run = get_latest_task_execution_for_task(
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
    """Prefer active definitions, then other non-terminal, newest log id first."""

    return sorted(
        targets,
        key=lambda target: (
            _cancel_status_rank(target.status),
            0 if target.destination else 1,
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
