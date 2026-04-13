"""Internal machine-state helpers for Unity task activations and runs.

This module keeps scheduled and triggerable task machine state inside the
existing Orchestra log/context system. The public `Unity/Tasks` table remains
the user-authored surface; `Tasks/Activations` and `Tasks/Runs` are internal
contexts derived from or driven by that surface.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

import httpx
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from orchestra.db.dao.context_dao import delete_orphaned_log_events
from orchestra.db.dao.unique_constraint_dao import UniqueConstraintDAO
from orchestra.db.models.orchestra_models import (
    Context,
    FieldType,
    LogEvent,
    LogEventContext,
    LogUniqueConstraint,
)

UNITY_TASKS_CONTEXT_NAME = "Tasks"
TASK_ACTIVATIONS_CONTEXT_NAME = "Tasks/Activations"
TASK_RUNS_CONTEXT_NAME = "Tasks/Runs"
_ALL_CONTEXT_SEGMENT = "All"
_TASK_ACTIVATION_UNIQUE_FIELD = "activation_key"
_TASK_RUN_UNIQUE_FIELD = "run_key"
_TASK_ACTIVATION_UPSERT_PATH = "/infra/task-activation/upsert"
_TASK_ACTIVATION_DELETE_PATH = "/infra/task-activation/delete"
_TASK_ACTIVATION_SYNC_TIMEOUT_SECONDS = 15.0
UNITY_TASK_CONTEXT_NAMES = {
    UNITY_TASKS_CONTEXT_NAME,
    TASK_ACTIVATIONS_CONTEXT_NAME,
    TASK_RUNS_CONTEXT_NAME,
}

_SCHEDULED_ACTIVATION_STATUSES = {"scheduled", "queued", "primed"}
_TRIGGERABLE_STATUS = "triggerable"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TaskMachineContextIds:
    """Resolved context identifiers for Unity task machine state."""

    activations_context_id: int
    runs_context_id: int


@dataclass(frozen=True)
class _TaskRow:
    """Minimal task row snapshot used by projector logic."""

    log_event_id: int
    data: dict[str, Any]
    updated_at: datetime | None
    created_at: datetime | None


def _split_context_name(context_name: str | None) -> list[str]:
    """Return non-empty path segments for a context name."""

    return [
        segment for segment in (context_name or "").strip("/").split("/") if segment
    ]


def _assistant_id_from_context_name(context_name: str | None) -> str | None:
    """Extract the assistant id from an assistant-scoped Unity tasks context."""

    segments = _split_context_name(context_name)
    if len(segments) < 2 or segments[-1] != UNITY_TASKS_CONTEXT_NAME:
        return None
    if segments[-2] == _ALL_CONTEXT_SEGMENT:
        return None
    return segments[-2]


def _resolve_assistant_id(
    *,
    task_row: _TaskRow | None = None,
    task_data: Mapping[str, Any] | None = None,
    tasks_context_name: str | None = None,
) -> str | None:
    """Resolve assistant ownership from row data first, then the context path."""

    candidate_data = task_row.data if task_row is not None else task_data
    if isinstance(candidate_data, Mapping):
        assistant_id = _coerce_optional_str(candidate_data.get("_assistant_id"))
        if assistant_id:
            return assistant_id
    return _assistant_id_from_context_name(tasks_context_name)


def _build_activation_key(*, assistant_id: str | None, task_id: int) -> str:
    """Return the assistant-scoped activation key used for uniqueness."""

    if assistant_id:
        return f"{assistant_id}:{task_id}"
    return str(task_id)


def is_unity_tasks_context_name(context_name: str | None) -> bool:
    """Return True when the name refers to the user-authored Unity tasks table."""

    segments = _split_context_name(context_name)
    if not segments or segments[-1] != UNITY_TASKS_CONTEXT_NAME:
        return False
    if len(segments) >= 2 and segments[-2] == _ALL_CONTEXT_SEGMENT:
        return False
    return not is_internal_task_machine_context_name(context_name)


def is_internal_task_machine_context_name(context_name: str | None) -> bool:
    """Return True when the name refers to an internal Unity task machine context."""

    normalized = (context_name or "").strip("/")
    return normalized in {TASK_ACTIVATIONS_CONTEXT_NAME, TASK_RUNS_CONTEXT_NAME}


def is_protected_unity_task_context_name(context_name: str | None) -> bool:
    """Return True for built-in Unity task contexts that should not be removed."""

    normalized = (context_name or "").strip("/")
    return is_unity_tasks_context_name(normalized) or normalized in {
        TASK_ACTIVATIONS_CONTEXT_NAME,
        TASK_RUNS_CONTEXT_NAME,
    }


_ACTIVATION_FIELD_DEFINITIONS: dict[str, dict[str, Any]] = {
    "assistant_id": {
        "field_type": "str",
        "mutable": False,
        "description": "Assistant identifier mirrored from the source Unity/Tasks row.",
    },
    "activation_key": {
        "field_type": "str",
        "mutable": False,
        "unique": True,
        "description": "Assistant-scoped unique key for the activation row.",
    },
    "task_id": {
        "field_type": "int",
        "mutable": False,
        "description": "Logical task identifier mirrored from Unity/Tasks.",
    },
    "source_task_log_id": {
        "field_type": "int",
        "mutable": True,
        "description": "Current Unity/Tasks row that owns this activation.",
    },
    "instance_id": {
        "field_type": "int",
        "mutable": True,
        "description": "Current task instance reflected into the activation row.",
    },
    "activation_kind": {
        "field_type": "str",
        "mutable": True,
        "description": "How the task wakes: scheduled or triggered.",
    },
    "execution_mode": {
        "field_type": "str",
        "mutable": True,
        "description": "Execution lane for the task: live or offline.",
    },
    "status": {
        "field_type": "str",
        "mutable": True,
        "description": "Source task status at the time this activation was projected.",
    },
    "next_due_at": {
        "field_type": "datetime",
        "mutable": True,
        "description": "Queue-head due timestamp for scheduled activations.",
    },
    "trigger_medium": {
        "field_type": "str",
        "mutable": True,
        "description": "Inbound medium required for trigger activations.",
    },
    "trigger_from_contact_ids": {
        "field_type": "list",
        "mutable": True,
        "description": "Optional allow-list of triggering contacts.",
    },
    "trigger_omit_contact_ids": {
        "field_type": "list",
        "mutable": True,
        "description": "Optional deny-list of triggering contacts.",
    },
    "interrupt": {
        "field_type": "bool",
        "mutable": True,
        "description": "Whether the trigger is allowed to interrupt active work.",
    },
    "trigger_recurring": {
        "field_type": "bool",
        "mutable": True,
        "description": "Whether the trigger re-arms after completion.",
    },
    "entrypoint": {
        "field_type": "str",
        "mutable": True,
        "description": "Offline entrypoint name when execution_mode=offline.",
    },
    "repeat": {
        "field_type": "list",
        "mutable": True,
        "description": "Recurring cadence metadata mirrored from the task row.",
    },
    "activation_revision": {
        "field_type": "str",
        "mutable": True,
        "description": "Stable hash of the machine-facing activation contract.",
    },
    "source_task_updated_at": {
        "field_type": "datetime",
        "mutable": True,
        "description": "Updated timestamp from the source Unity/Tasks row.",
    },
    "last_materialized_at": {
        "field_type": "datetime",
        "mutable": True,
        "description": "When Orchestra last projected this activation row.",
    },
}

_RUN_FIELD_DEFINITIONS: dict[str, dict[str, Any]] = {
    "assistant_id": {
        "field_type": "str",
        "mutable": False,
        "description": "Assistant identifier that owns this run.",
    },
    "run_id": {
        "field_type": "int",
        "mutable": False,
        "description": "Stable internal run identifier (matches the log_event id).",
    },
    "run_key": {
        "field_type": "str",
        "mutable": False,
        "unique": True,
        "description": "Idempotency key for a task execution attempt.",
    },
    "task_id": {
        "field_type": "int",
        "mutable": False,
        "description": "Logical task identifier for the run.",
    },
    "source_task_log_id": {
        "field_type": "int",
        "mutable": True,
        "description": "Unity/Tasks row that originated this run.",
    },
    "source_type": {
        "field_type": "str",
        "mutable": True,
        "description": "Why the run exists: scheduled, triggered, or offline dispatch.",
    },
    "execution_mode": {
        "field_type": "str",
        "mutable": True,
        "description": "Execution lane for the run: live or offline.",
    },
    "state": {
        "field_type": "str",
        "mutable": True,
        "description": "Current machine state for the run lifecycle.",
    },
    "activation_revision": {
        "field_type": "str",
        "mutable": True,
        "description": "Activation revision adopted when this run was created.",
    },
    "scheduled_for": {
        "field_type": "datetime",
        "mutable": True,
        "description": "Scheduled timestamp when the run originated from a due activation.",
    },
    "started_at": {
        "field_type": "datetime",
        "mutable": True,
        "description": "Run start timestamp.",
    },
    "completed_at": {
        "field_type": "datetime",
        "mutable": True,
        "description": "Run completion timestamp.",
    },
    "result_summary": {
        "field_type": "str",
        "mutable": True,
        "description": "Hidden internal outcome summary for the run.",
    },
    "error": {
        "field_type": "str",
        "mutable": True,
        "description": "Hidden internal error payload for failed runs.",
    },
}


def ensure_task_machine_contexts(
    session: Session,
    project_id: int,
) -> TaskMachineContextIds:
    """Ensure the Unity task machine contexts and field schemas exist."""

    activations_context_id = _upsert_context(
        session=session,
        project_id=project_id,
        name=TASK_ACTIVATIONS_CONTEXT_NAME,
        description="Internal machine-facing activation state for Unity tasks.",
        allow_duplicates=False,
        unique_keys={_TASK_ACTIVATION_UNIQUE_FIELD: "str"},
    )
    runs_context_id = _upsert_context(
        session=session,
        project_id=project_id,
        name=TASK_RUNS_CONTEXT_NAME,
        description="Internal idempotent execution history for Unity tasks.",
        allow_duplicates=False,
        unique_keys={_TASK_RUN_UNIQUE_FIELD: "str"},
    )
    _upsert_field_types(
        session=session,
        project_id=project_id,
        context_id=activations_context_id,
        field_definitions=_ACTIVATION_FIELD_DEFINITIONS,
    )
    _upsert_field_types(
        session=session,
        project_id=project_id,
        context_id=runs_context_id,
        field_definitions=_RUN_FIELD_DEFINITIONS,
    )
    session.flush()
    return TaskMachineContextIds(
        activations_context_id=activations_context_id,
        runs_context_id=runs_context_id,
    )


def sync_task_activations_for_task_ids(
    session: Session,
    project_id: int,
    task_ids: Iterable[int],
    *,
    tasks_context_name: str = UNITY_TASKS_CONTEXT_NAME,
) -> dict[str, int]:
    """Project one assistant-scoped Unity tasks table into `Tasks/Activations`."""

    unique_task_ids = sorted({int(task_id) for task_id in task_ids})
    if not unique_task_ids or not is_unity_tasks_context_name(tasks_context_name):
        return {"upserted": 0, "deleted": 0}

    tasks_context_id = _get_context_id(
        session=session,
        project_id=project_id,
        name=tasks_context_name,
    )
    if tasks_context_id is None:
        return {"upserted": 0, "deleted": 0}

    context_ids = ensure_task_machine_contexts(session=session, project_id=project_id)
    task_rows = _load_task_rows(
        session=session,
        project_id=project_id,
        context_id=tasks_context_id,
        task_ids=unique_task_ids,
    )
    rows_by_task_id: dict[int, list[_TaskRow]] = {
        task_id: [] for task_id in unique_task_ids
    }
    for row in task_rows:
        task_id = _coerce_int(row.data.get("task_id"))
        if task_id is not None and task_id in rows_by_task_id:
            rows_by_task_id[task_id].append(row)

    upserted = 0
    deleted = 0
    materialization_pairs: list[tuple[dict[str, Any] | None, dict[str, Any] | None]] = (
        []
    )
    for task_id in unique_task_ids:
        activation_key = _build_activation_key(
            assistant_id=_resolve_assistant_id(
                task_data=(
                    (rows_by_task_id.get(task_id) or [None])[0].data
                    if rows_by_task_id.get(task_id)
                    else None
                ),
                tasks_context_name=tasks_context_name,
            ),
            task_id=task_id,
        )
        existing_activation = _get_machine_row_by_unique_field(
            session=session,
            context_id=context_ids.activations_context_id,
            unique_field_name=_TASK_ACTIVATION_UNIQUE_FIELD,
            unique_field_value=activation_key,
        )
        previous_activation = (
            dict(existing_activation.data or {})
            if existing_activation is not None
            else None
        )
        activation_payload = _build_activation_payload(
            rows=rows_by_task_id.get(task_id, []),
            tasks_context_name=tasks_context_name,
        )
        if activation_payload is None:
            was_deleted = _delete_machine_row_by_unique_field(
                session=session,
                project_id=project_id,
                context_id=context_ids.activations_context_id,
                unique_field_name=_TASK_ACTIVATION_UNIQUE_FIELD,
                unique_field_value=activation_key,
            )
            deleted += int(was_deleted)
            if previous_activation is not None and was_deleted:
                materialization_pairs.append((previous_activation, None))
            continue

        _upsert_machine_row(
            session=session,
            project_id=project_id,
            context_id=context_ids.activations_context_id,
            unique_field_name=_TASK_ACTIVATION_UNIQUE_FIELD,
            unique_field_value=activation_key,
            payload=activation_payload,
        )
        materialization_pairs.append((previous_activation, activation_payload))
        upserted += 1

    session.flush()
    for previous_activation, current_activation in materialization_pairs:
        _reconcile_scheduled_activation_materialization(
            previous_activation=previous_activation,
            current_activation=current_activation,
        )
    return {"upserted": upserted, "deleted": deleted}


def get_task_activation(
    session: Session,
    project_id: int,
    *,
    assistant_id: str | None,
    task_id: int,
) -> LogEvent | None:
    """Return the current activation row for one assistant/task pair, if present."""

    context_ids = ensure_task_machine_contexts(session=session, project_id=project_id)
    return _get_machine_row_by_unique_field(
        session=session,
        context_id=context_ids.activations_context_id,
        unique_field_name=_TASK_ACTIVATION_UNIQUE_FIELD,
        unique_field_value=_build_activation_key(
            assistant_id=assistant_id,
            task_id=task_id,
        ),
    )


def create_task_run_if_absent(
    session: Session,
    project_id: int,
    payload: Mapping[str, Any],
) -> tuple[LogEvent, bool]:
    """Create a task run row by `run_key` if it does not already exist."""

    run_key = payload.get("run_key")
    if not isinstance(run_key, str) or not run_key:
        raise ValueError("Task run payload must include a non-empty run_key.")

    context_ids = ensure_task_machine_contexts(session=session, project_id=project_id)
    existing = _get_machine_row_by_unique_field(
        session=session,
        context_id=context_ids.runs_context_id,
        unique_field_name=_TASK_RUN_UNIQUE_FIELD,
        unique_field_value=run_key,
    )
    if existing is not None:
        return existing, False

    materialized_payload = dict(payload)
    materialized_payload.setdefault("state", "pending")
    created = _upsert_machine_row(
        session=session,
        project_id=project_id,
        context_id=context_ids.runs_context_id,
        unique_field_name=_TASK_RUN_UNIQUE_FIELD,
        unique_field_value=run_key,
        payload=materialized_payload,
    )
    if created.data.get("run_id") != created.id:
        created_payload = dict(created.data or {})
        created_payload["run_id"] = created.id
        _replace_log_payload(created, created_payload)
    session.flush()
    return created, True


def update_task_run(
    session: Session,
    project_id: int,
    run_key: str,
    updates: Mapping[str, Any],
) -> LogEvent:
    """Apply a partial update to an existing task run row."""

    context_ids = ensure_task_machine_contexts(session=session, project_id=project_id)
    existing = _get_machine_row_by_unique_field(
        session=session,
        context_id=context_ids.runs_context_id,
        unique_field_name=_TASK_RUN_UNIQUE_FIELD,
        unique_field_value=run_key,
    )
    if existing is None:
        raise ValueError(f"Task run with run_key='{run_key}' not found.")

    payload = dict(existing.data or {})
    payload.update(dict(updates))
    payload.setdefault("run_id", existing.id)
    _replace_log_payload(existing, payload)
    session.flush()
    return existing


def get_task_run(
    session: Session,
    project_id: int,
    run_key: str,
) -> LogEvent | None:
    """Return an existing task run row by run_key."""

    context_ids = ensure_task_machine_contexts(session=session, project_id=project_id)
    return _get_machine_row_by_unique_field(
        session=session,
        context_id=context_ids.runs_context_id,
        unique_field_name=_TASK_RUN_UNIQUE_FIELD,
        unique_field_value=run_key,
    )


def get_task_ids_for_log_ids(
    session: Session,
    project_id: int,
    *,
    context_name: str,
    log_event_ids: Iterable[int],
) -> set[int]:
    """Return logical task ids for the specified Unity/Tasks log rows."""

    ids = [int(log_id) for log_id in set(log_event_ids)]
    if not ids:
        return set()

    context_id = _get_context_id(
        session=session,
        project_id=project_id,
        name=context_name,
    )
    if context_id is None:
        return set()

    rows = (
        session.query(LogEvent.data)
        .join(LogEventContext, LogEventContext.log_event_id == LogEvent.id)
        .filter(
            LogEvent.project_id == project_id,
            LogEvent.id.in_(ids),
            LogEventContext.context_id == context_id,
        )
        .all()
    )
    task_ids: set[int] = set()
    for (data,) in rows:
        if isinstance(data, dict):
            task_id = _coerce_int(data.get("task_id"))
            if task_id is not None:
                task_ids.add(task_id)
    return task_ids


def _build_activation_payload(
    rows: Sequence[_TaskRow],
    *,
    tasks_context_name: str,
) -> dict[str, Any] | None:
    """Choose the current activatable task instance and project its machine facts."""

    if not rows:
        return None

    ordered_rows = sorted(
        rows,
        key=lambda row: (
            row.updated_at
            or row.created_at
            or datetime.min.replace(tzinfo=timezone.utc),
            _coerce_int(row.data.get("instance_id")) or -1,
            row.log_event_id,
        ),
        reverse=True,
    )
    scheduled_candidates = [
        row for row in ordered_rows if _is_scheduled_activation_candidate(row.data)
    ]
    if scheduled_candidates:
        return _project_activation_payload(
            row=scheduled_candidates[0],
            activation_kind="scheduled",
            tasks_context_name=tasks_context_name,
        )

    trigger_candidates = [
        row for row in ordered_rows if _is_trigger_activation_candidate(row.data)
    ]
    if trigger_candidates:
        return _project_activation_payload(
            row=trigger_candidates[0],
            activation_kind="triggered",
            tasks_context_name=tasks_context_name,
        )

    return None


def _project_activation_payload(
    row: _TaskRow,
    *,
    activation_kind: str,
    tasks_context_name: str,
) -> dict[str, Any]:
    """Flatten the chosen source task row into an activation payload."""

    task_id = _coerce_int(row.data.get("task_id"))
    if task_id is None:
        raise ValueError("Activations require task rows with an integer task_id.")

    assistant_id = _resolve_assistant_id(
        task_row=row,
        tasks_context_name=tasks_context_name,
    )
    schedule = (
        row.data.get("schedule") if isinstance(row.data.get("schedule"), dict) else {}
    )
    trigger = (
        row.data.get("trigger") if isinstance(row.data.get("trigger"), dict) else {}
    )
    payload = {
        "assistant_id": assistant_id,
        "activation_key": _build_activation_key(
            assistant_id=assistant_id,
            task_id=task_id,
        ),
        "task_id": task_id,
        "source_task_log_id": row.log_event_id,
        "instance_id": _coerce_int(row.data.get("instance_id")),
        "activation_kind": activation_kind,
        "execution_mode": "offline" if bool(row.data.get("offline")) else "live",
        "status": row.data.get("status"),
        "next_due_at": _coerce_datetime_string(schedule.get("start_at")),
        "trigger_medium": _coerce_optional_str(trigger.get("medium")),
        "trigger_from_contact_ids": _coerce_optional_list(
            trigger.get("from_contact_ids"),
        ),
        "trigger_omit_contact_ids": _coerce_optional_list(
            trigger.get("omit_contact_ids"),
        ),
        "interrupt": bool(trigger.get("interrupt", False)),
        "trigger_recurring": bool(trigger.get("recurring", False)),
        "entrypoint": _coerce_optional_str(row.data.get("entrypoint")),
        "repeat": _coerce_optional_list(row.data.get("repeat")),
        "source_task_updated_at": _coerce_datetime_string(
            row.updated_at or row.created_at,
        ),
    }
    payload["activation_revision"] = _stable_hash(payload)
    payload["last_materialized_at"] = _coerce_datetime_string(
        datetime.now(timezone.utc),
    )
    return payload


def _reconcile_scheduled_activation_materialization(
    *,
    previous_activation: Mapping[str, Any] | None,
    current_activation: Mapping[str, Any] | None,
) -> None:
    """Mirror scheduled activation changes into Communication's delayed queue."""

    current_upsert_body = _scheduled_activation_upsert_body(current_activation)
    previous_delete_body = _scheduled_activation_delete_body(previous_activation)
    if current_upsert_body is not None:
        if previous_delete_body is not None:
            current_upsert_body["previous_activation_revision"] = previous_delete_body[
                "activation_revision"
            ]
            current_upsert_body["previous_scheduled_for"] = previous_delete_body[
                "scheduled_for"
            ]
        _post_task_activation_request(
            path=_TASK_ACTIVATION_UPSERT_PATH,
            body=current_upsert_body,
        )
        return
    if previous_delete_body is not None:
        _post_task_activation_request(
            path=_TASK_ACTIVATION_DELETE_PATH,
            body=previous_delete_body,
        )


def _scheduled_activation_upsert_body(
    activation: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Build the Communication upsert payload for one scheduled activation."""

    if not _is_scheduled_activation_payload(activation):
        return None
    assistant_id = _coerce_optional_str(activation.get("assistant_id"))
    task_id = _coerce_int(activation.get("task_id"))
    source_task_log_id = _coerce_int(activation.get("source_task_log_id"))
    activation_revision = _coerce_optional_str(activation.get("activation_revision"))
    scheduled_for = _coerce_datetime_string(activation.get("next_due_at"))
    if not assistant_id or task_id is None or source_task_log_id is None:
        return None
    if not activation_revision or not scheduled_for:
        return None
    return {
        "assistant_id": assistant_id,
        "task_id": task_id,
        "source_task_log_id": source_task_log_id,
        "activation_revision": activation_revision,
        "scheduled_for": scheduled_for,
        "execution_mode": _coerce_optional_str(activation.get("execution_mode"))
        or "live",
        "source_type": "scheduled",
    }


def _scheduled_activation_delete_body(
    activation: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Build the Communication delete payload for one scheduled activation."""

    if not _is_scheduled_activation_payload(activation):
        return None
    assistant_id = _coerce_optional_str(activation.get("assistant_id"))
    task_id = _coerce_int(activation.get("task_id"))
    activation_revision = _coerce_optional_str(activation.get("activation_revision"))
    scheduled_for = _coerce_datetime_string(activation.get("next_due_at"))
    if (
        not assistant_id
        or task_id is None
        or not activation_revision
        or not scheduled_for
    ):
        return None
    return {
        "assistant_id": assistant_id,
        "task_id": task_id,
        "activation_revision": activation_revision,
        "scheduled_for": scheduled_for,
    }


def _is_scheduled_activation_payload(activation: Mapping[str, Any] | None) -> bool:
    """Return True when the payload represents a scheduled activation snapshot."""

    if not isinstance(activation, Mapping):
        return False
    return _coerce_optional_str(activation.get("activation_kind")) == "scheduled"


def _post_task_activation_request(*, path: str, body: Mapping[str, Any]) -> None:
    """Send one activation sync request to Communication when configured."""

    comms_url = os.environ.get("UNITY_COMMS_URL", "").rstrip("/")
    admin_key = os.environ.get("ORCHESTRA_ADMIN_KEY", "")
    if not comms_url or not admin_key:
        logger.info(
            "Skipping task activation sync because UNITY_COMMS_URL or ORCHESTRA_ADMIN_KEY is missing.",
        )
        return
    with httpx.Client() as client:
        response = client.post(
            f"{comms_url}{path}",
            headers={"Authorization": f"Bearer {admin_key}"},
            json=dict(body),
            timeout=_TASK_ACTIVATION_SYNC_TIMEOUT_SECONDS,
        )
        response.raise_for_status()


def _is_scheduled_activation_candidate(data: Mapping[str, Any]) -> bool:
    """Return True when a task row is the current armed scheduled activation."""

    schedule = data.get("schedule")
    trigger = data.get("trigger")
    if trigger not in (None, {}):
        return False
    if not isinstance(schedule, dict):
        return False
    if schedule.get("prev_task") is not None:
        return False
    if schedule.get("start_at") is None:
        return False
    status = _coerce_optional_str(data.get("status"))
    return status in _SCHEDULED_ACTIVATION_STATUSES


def _is_trigger_activation_candidate(data: Mapping[str, Any]) -> bool:
    """Return True when a task row is the current armed trigger activation."""

    schedule = data.get("schedule")
    trigger = data.get("trigger")
    if schedule not in (None, {}):
        return False
    if not isinstance(trigger, dict):
        return False
    status = _coerce_optional_str(data.get("status"))
    return status == _TRIGGERABLE_STATUS


def _load_task_rows(
    session: Session,
    *,
    project_id: int,
    context_id: int,
    task_ids: Sequence[int],
) -> list[_TaskRow]:
    """Load Unity/Tasks rows for the given logical task ids."""

    task_id_strings = [str(task_id) for task_id in task_ids]
    rows = (
        session.query(
            LogEvent.id,
            LogEvent.data,
            LogEvent.updated_at,
            LogEvent.created_at,
        )
        .join(LogEventContext, LogEventContext.log_event_id == LogEvent.id)
        .filter(
            LogEvent.project_id == project_id,
            LogEventContext.context_id == context_id,
            LogEvent.data.has_key("task_id"),
            LogEvent.data.op("->>")("task_id").in_(task_id_strings),
        )
        .all()
    )
    return [
        _TaskRow(
            log_event_id=log_event_id,
            data=data if isinstance(data, dict) else {},
            updated_at=updated_at,
            created_at=created_at,
        )
        for log_event_id, data, updated_at, created_at in rows
    ]


def _upsert_context(
    session: Session,
    *,
    project_id: int,
    name: str,
    description: str,
    allow_duplicates: bool,
    unique_keys: dict[str, str] | None = None,
) -> int:
    """Create or reconcile a context without forcing an early commit."""

    normalized_name = name.strip("/")
    existing = session.execute(
        select(Context).where(
            Context.project_id == project_id,
            Context.name == normalized_name,
        ),
    ).scalar_one_or_none()
    if existing is None:
        now = datetime.now(timezone.utc)
        stmt = (
            pg_insert(Context)
            .values(
                project_id=project_id,
                name=normalized_name,
                description=description,
                created_at=now,
                updated_at=now,
                is_versioned=False,
                allow_duplicates=allow_duplicates,
                unique_key_names=list((unique_keys or {}).keys()),
                unique_key_types=list((unique_keys or {}).values()),
                auto_counting={},
                foreign_keys=[],
            )
            .on_conflict_do_nothing(index_elements=["project_id", "name"])
            .returning(Context.id)
        )
        context_id = session.execute(stmt).scalar()
        if context_id is not None:
            return int(context_id)

        existing = session.execute(
            select(Context).where(
                Context.project_id == project_id,
                Context.name == normalized_name,
            ),
        ).scalar_one()

    desired_names = list((unique_keys or {}).keys())
    desired_types = list((unique_keys or {}).values())
    if (
        existing.description != description
        or bool(existing.allow_duplicates) != allow_duplicates
        or list(existing.unique_key_names or []) != desired_names
        or list(existing.unique_key_types or []) != desired_types
    ):
        existing.description = description
        existing.allow_duplicates = allow_duplicates
        existing.unique_key_names = desired_names
        existing.unique_key_types = desired_types
        session.flush()
    return int(existing.id)


def _upsert_field_types(
    session: Session,
    *,
    project_id: int,
    context_id: int,
    field_definitions: Mapping[str, Mapping[str, Any]],
) -> None:
    """Upsert machine field definitions for an internal context."""

    if not field_definitions:
        return

    values = []
    for field_name, definition in field_definitions.items():
        values.append(
            {
                "project_id": project_id,
                "field_name": field_name,
                "field_type": definition["field_type"],
                "field_category": "entry",
                "mutable": definition.get("mutable", True),
                "unique": definition.get("unique", False),
                "context_id": context_id,
                "enum_values": None,
                "enum_restrict": False,
                "description": definition.get("description"),
            },
        )

    stmt = pg_insert(FieldType).values(values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["project_id", "field_name", "context_id"],
        set_={
            "field_type": stmt.excluded.field_type,
            "field_category": stmt.excluded.field_category,
            "mutable": stmt.excluded.mutable,
            "unique": stmt.excluded.unique,
            "enum_values": stmt.excluded.enum_values,
            "enum_restrict": stmt.excluded.enum_restrict,
            "description": stmt.excluded.description,
        },
    )
    session.execute(stmt)


def _upsert_machine_row(
    session: Session,
    *,
    project_id: int,
    context_id: int,
    unique_field_name: str,
    unique_field_value: int | str,
    payload: Mapping[str, Any],
) -> LogEvent:
    """Create or replace an internal machine row using a logical unique key."""

    existing = _get_machine_row_by_unique_field(
        session=session,
        context_id=context_id,
        unique_field_name=unique_field_name,
        unique_field_value=unique_field_value,
    )
    if existing is not None:
        _replace_log_payload(existing, payload)
        return existing

    now = datetime.now(timezone.utc)
    log_event = LogEvent(
        project_id=project_id,
        data=dict(payload),
        key_order=_extract_key_order(dict(payload)),
        created_at=now,
        updated_at=now,
    )
    session.add(log_event)
    session.flush()

    session.add(
        LogEventContext(
            log_event_id=log_event.id,
            context_id=context_id,
        ),
    )
    session.flush()

    inserted = session.execute(
        pg_insert(LogUniqueConstraint)
        .values(
            context_id=context_id,
            field_name=unique_field_name,
            value_hash=UniqueConstraintDAO.hash_value(unique_field_value),
            log_event_id=log_event.id,
        )
        .on_conflict_do_nothing(
            index_elements=["context_id", "field_name", "value_hash"],
        )
        .returning(LogUniqueConstraint.log_event_id),
    ).scalar()
    if inserted is not None:
        return log_event

    session.execute(
        delete(LogEventContext).where(
            LogEventContext.log_event_id == log_event.id,
            LogEventContext.context_id == context_id,
        ),
    )
    delete_orphaned_log_events(
        session=session,
        project_id=project_id,
        skip_embedding_cleanup=True,
        log_event_ids=[log_event.id],
    )
    existing = _get_machine_row_by_unique_field(
        session=session,
        context_id=context_id,
        unique_field_name=unique_field_name,
        unique_field_value=unique_field_value,
    )
    if existing is None:
        raise ValueError(
            f"Failed to resolve machine row for {unique_field_name}={unique_field_value!r}.",
        )
    return existing


def _delete_machine_row_by_unique_field(
    session: Session,
    *,
    project_id: int,
    context_id: int,
    unique_field_name: str,
    unique_field_value: int | str,
) -> bool:
    """Delete an internal machine row and its uniqueness metadata if present."""

    existing = _get_machine_row_by_unique_field(
        session=session,
        context_id=context_id,
        unique_field_name=unique_field_name,
        unique_field_value=unique_field_value,
    )
    if existing is None:
        return False

    session.execute(
        delete(LogUniqueConstraint).where(
            LogUniqueConstraint.log_event_id == existing.id,
        ),
    )
    session.execute(
        delete(LogEventContext).where(
            LogEventContext.log_event_id == existing.id,
            LogEventContext.context_id == context_id,
        ),
    )
    delete_orphaned_log_events(
        session=session,
        project_id=project_id,
        skip_embedding_cleanup=True,
        log_event_ids=[existing.id],
    )
    session.flush()
    return True


def _get_machine_row_by_unique_field(
    session: Session,
    *,
    context_id: int,
    unique_field_name: str,
    unique_field_value: int | str,
) -> LogEvent | None:
    """Return a machine row by a top-level unique field value."""

    rows = (
        session.query(LogEvent)
        .join(LogEventContext, LogEventContext.log_event_id == LogEvent.id)
        .filter(
            LogEventContext.context_id == context_id,
            LogEvent.data.has_key(unique_field_name),
            LogEvent.data.op("->>")(unique_field_name) == str(unique_field_value),
        )
        .order_by(LogEvent.updated_at.desc().nullslast(), LogEvent.id.desc())
        .all()
    )
    if not rows:
        return None
    return rows[0]


def _replace_log_payload(log_event: LogEvent, payload: Mapping[str, Any]) -> None:
    """Replace an internal machine row payload while preserving key order."""

    log_event.data = dict(payload)
    log_event.key_order = _extract_key_order(dict(payload))
    log_event.updated_at = datetime.now(timezone.utc)


def _require_context_id(
    session: Session,
    *,
    project_id: int,
    context_name: str,
) -> int:
    """Return an existing context id or raise when the expected context is absent."""

    context_id = _get_context_id(
        session=session,
        project_id=project_id,
        name=context_name,
    )
    if context_id is None:
        raise ValueError(
            f"Expected context '{context_name}' to exist for project_id={project_id}.",
        )
    return context_id


def _get_context_id(
    session: Session,
    *,
    project_id: int,
    name: str,
) -> int | None:
    """Return a context id for a project/name pair when present."""

    return session.execute(
        select(Context.id).where(
            Context.project_id == project_id,
            Context.name == name.strip("/"),
        ),
    ).scalar_one_or_none()


def _coerce_int(value: Any) -> int | None:
    """Best-effort integer coercion for JSON-backed task rows."""

    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_optional_str(value: Any) -> str | None:
    """Convert a value to string when present, preserving None."""

    if value is None:
        return None
    return str(value)


def _coerce_optional_list(value: Any) -> list[Any] | None:
    """Normalize list-like JSON values while preserving None."""

    if value is None:
        return None
    if isinstance(value, list):
        return value
    return [value]


def _coerce_datetime_string(value: Any) -> str | None:
    """Convert datetime-like values to ISO-8601 strings."""

    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _stable_hash(value: Mapping[str, Any]) -> str:
    """Create a deterministic content hash for activation revisions."""

    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.md5(canonical.encode("utf-8")).hexdigest()


def _extract_key_order(data: Any, path: str = "_root") -> dict[str, list[str]]:
    """Recursively preserve nested dict insertion order for JSONB log rows."""

    result: dict[str, list[str]] = {}
    if isinstance(data, dict) and data:
        result[path] = list(data.keys())
        for key, value in data.items():
            child_path = key if path == "_root" else f"{path}.{key}"
            result.update(_extract_key_order(value, child_path))
    elif isinstance(data, list):
        for index, item in enumerate(data):
            if isinstance(item, dict):
                result.update(_extract_key_order(item, f"{path}[{index}]"))
    return result
