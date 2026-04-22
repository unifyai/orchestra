"""Internal machine-state helpers for assistant task activations and runs.

This module keeps scheduled and triggerable task machine state inside the
existing Orchestra log/context system. The public assistant-scoped `.../Tasks`
table in the `Assistants` project remains the user-authored surface;
`Tasks/Activations`, `Tasks/Runs`, and `Tasks/OutboundOperations` are internal contexts derived from or
driven by that surface.
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
    Assistant,
    Context,
    FieldType,
    LogEvent,
    LogEventContext,
    LogUniqueConstraint,
)

TASK_MACHINE_PROJECT_NAME = "Assistants"
TASKS_CONTEXT_NAME = "Tasks"
TASK_ACTIVATIONS_CONTEXT_NAME = "Tasks/Activations"
TASK_RUNS_CONTEXT_NAME = "Tasks/Runs"
TASK_OUTBOUND_OPERATIONS_CONTEXT_NAME = "Tasks/OutboundOperations"
HIVE_CONTEXT_PREFIX = "Hives/"
_ALL_CONTEXT_SEGMENT = "All"
_TASK_ACTIVATIONS_CONTEXT_LEAF = "Activations"
_TASK_RUNS_CONTEXT_LEAF = "Runs"
_TASK_OUTBOUND_OPERATIONS_CONTEXT_LEAF = "OutboundOperations"
_TASK_ACTIVATION_UNIQUE_FIELD = "activation_key"
_TASK_RUN_UNIQUE_FIELD = "run_key"
_TASK_OUTBOUND_OPERATION_UNIQUE_FIELD = "operation_key"
_TASK_ACTIVATION_UPSERT_PATH = "/infra/task-activation/upsert"
_TASK_ACTIVATION_DELETE_PATH = "/infra/task-activation/delete"
_TASK_ACTIVATION_SYNC_TIMEOUT_SECONDS = 15.0
_INTERNAL_TASK_MACHINE_CONTEXT_NAMES = frozenset(
    {
        TASK_ACTIVATIONS_CONTEXT_NAME,
        TASK_RUNS_CONTEXT_NAME,
        TASK_OUTBOUND_OPERATIONS_CONTEXT_NAME,
    },
)

_SCHEDULED_ACTIVATION_STATUSES = {"scheduled", "queued", "primed"}
_TRIGGERABLE_STATUS = "triggerable"
_DEFAULT_SCHEDULED_TASK_VISIBILITY_POLICY = "silent_by_default"
_RECURRING_WAKE_HINT = "recurring"
_ONE_OFF_WAKE_HINT = "one_off"
_TASK_SUMMARY_MAX_CHARS = 240

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TaskMachineContextIds:
    """Resolved context identifiers for task machine state."""

    activations_context_id: int
    runs_context_id: int
    outbound_operations_context_id: int


@dataclass(frozen=True)
class TaskMachineContextNames:
    """Resolved assistant-scoped context names for task machine state."""

    tasks_context_name: str
    activations_context_name: str
    runs_context_name: str
    outbound_operations_context_name: str


@dataclass(frozen=True)
class _MachineRowUpsertResult:
    """One internal machine-row upsert outcome."""

    row: LogEvent
    created: bool


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
    """Extract the assistant id from an assistant-scoped tasks context.

    Hive paths (``Hives/{hive_id}/Tasks`` and any deeper Hive subtree) never
    name an assistant — their second segment is a hive id — so the parser
    refuses them outright. Callers that need the owning body for a Hive row
    must read ``_assistant_id`` off the row's data instead of inferring it
    from the path.
    """

    normalized = (context_name or "").strip("/")
    if normalized.startswith(HIVE_CONTEXT_PREFIX):
        return None
    segments = _split_context_name(normalized)
    if len(segments) < 2 or segments[-1] != TASKS_CONTEXT_NAME:
        return None
    if segments[-2] == _ALL_CONTEXT_SEGMENT:
        return None
    return segments[-2]


def build_task_activation_context_name(tasks_context_name: str) -> str:
    """Return the assistant-scoped activations context for one Tasks table."""

    return _build_task_machine_context_name(
        tasks_context_name=tasks_context_name,
        leaf_name=_TASK_ACTIVATIONS_CONTEXT_LEAF,
    )


def build_task_runs_context_name(tasks_context_name: str) -> str:
    """Return the assistant-scoped runs context for one Tasks table."""

    return _build_task_machine_context_name(
        tasks_context_name=tasks_context_name,
        leaf_name=_TASK_RUNS_CONTEXT_LEAF,
    )


def build_task_outbound_operations_context_name(tasks_context_name: str) -> str:
    """Return the assistant-scoped outbound-operations context for one Tasks table."""

    return _build_task_machine_context_name(
        tasks_context_name=tasks_context_name,
        leaf_name=_TASK_OUTBOUND_OPERATIONS_CONTEXT_LEAF,
    )


def _build_task_machine_context_name(*, tasks_context_name: str, leaf_name: str) -> str:
    """Return one assistant-scoped internal context derived from `.../Tasks`."""

    normalized_tasks_context_name = (tasks_context_name or "").strip("/")
    if not is_task_surface_context_name(normalized_tasks_context_name):
        raise ValueError(
            f"Expected an assistant-scoped Tasks context, got {tasks_context_name!r}.",
        )
    return f"{normalized_tasks_context_name}/{leaf_name}"


def _resolve_task_machine_context_names(
    tasks_context_name: str,
) -> TaskMachineContextNames:
    """Return the assistant-scoped machine-state context names for one Tasks table."""

    normalized_tasks_context_name = (tasks_context_name or "").strip("/")
    return TaskMachineContextNames(
        tasks_context_name=normalized_tasks_context_name,
        activations_context_name=build_task_activation_context_name(
            normalized_tasks_context_name,
        ),
        runs_context_name=build_task_runs_context_name(
            normalized_tasks_context_name,
        ),
        outbound_operations_context_name=build_task_outbound_operations_context_name(
            normalized_tasks_context_name,
        ),
    )


def _build_task_machine_context_names_for_owner(
    *,
    user_id: str,
    assistant_id: str,
) -> TaskMachineContextNames:
    """Return the machine-state context names for one owning body."""

    tasks_context_name = _build_assistant_tasks_context_name(
        user_id=user_id,
        assistant_id=assistant_id,
    )
    return _resolve_task_machine_context_names(tasks_context_name)


def _split_per_body_tasks_context_name(
    tasks_context_name: str,
) -> tuple[str, str]:
    """Return ``(user_id, assistant_id)`` for a canonical per-body Tasks path.

    Raises ``ValueError`` for Hive paths or any path shorter than
    ``{user}/{assistant}/Tasks``; callers that expect to handle Hive-scoped
    surfaces resolve owners from row data instead.
    """

    normalized = (tasks_context_name or "").strip("/")
    if normalized.startswith(HIVE_CONTEXT_PREFIX):
        raise ValueError(
            "Hive tasks contexts do not encode a per-body owner; resolve "
            f"the owning assistant from row data instead (got {tasks_context_name!r}).",
        )
    segments = _split_context_name(normalized)
    if len(segments) < 3 or segments[-1] != TASKS_CONTEXT_NAME:
        raise ValueError(
            f"Expected an assistant-scoped Tasks context, got {tasks_context_name!r}.",
        )
    assistant_id = segments[-2]
    user_id = "/".join(segments[:-2])
    return user_id, assistant_id


def _resolve_assistant_id(
    *,
    task_row: _TaskRow | None = None,
    task_data: Mapping[str, Any] | None = None,
    tasks_context_name: str | None = None,
) -> str | None:
    """Resolve assistant ownership from row data first, then the context path.

    Hive-scoped task rows do not encode the owning body in their path, so the
    row's ``_assistant_id`` column is the only authoritative signal. When a
    Hive path arrives without that column populated we raise loudly rather
    than silently falling back to path inference, which would either return
    ``None`` (misrouting machine state) or mistake the hive id for an
    assistant id.
    """

    candidate_data = task_row.data if task_row is not None else task_data
    if isinstance(candidate_data, Mapping):
        assistant_id = _coerce_optional_str(candidate_data.get("_assistant_id"))
        if assistant_id:
            return assistant_id
    if tasks_context_name and tasks_context_name.startswith(HIVE_CONTEXT_PREFIX):
        log_event_id = task_row.log_event_id if task_row is not None else None
        raise ValueError(
            f"Hive task row missing _assistant_id: log_event_id={log_event_id}",
        )
    return _assistant_id_from_context_name(tasks_context_name)


def _build_activation_key(*, assistant_id: str | None, task_id: int) -> str:
    """Return the assistant-scoped activation key used for uniqueness."""

    if assistant_id:
        return f"{assistant_id}:{task_id}"
    return str(task_id)


def is_task_surface_context_name(context_name: str | None) -> bool:
    """Return True when the name refers to the user-authored tasks table.

    This predicate is the projection trigger for ``_sync_task_activations_if_needed``:
    any ``/logs`` write whose context is a task-surface name causes the
    task-machine service to re-materialize activations. It deliberately does
    NOT reject Hive paths — ``Hives/{hive_id}/Tasks`` is the Hive-shared
    definition surface and every write to it must fire projection so each
    body's ``{user}/{assistant}/Tasks/Activations`` stays in sync. The
    asymmetry with :func:`is_internal_task_machine_context_name` and
    :func:`is_protected_task_surface_context_name` is load-bearing: those two
    classifiers reject Hive paths so nothing under ``Hives/`` is ever treated
    as live machine state, while projection correctness for Hive-defined
    tasks is secured by the per-row ``_assistant_id`` owner stamp and the
    grouped projection path in :func:`sync_task_activations_for_task_ids`.
    """

    segments = _split_context_name(context_name)
    if not segments or segments[-1] != TASKS_CONTEXT_NAME:
        return False
    if len(segments) >= 2 and segments[-2] == _ALL_CONTEXT_SEGMENT:
        return False
    return not is_internal_task_machine_context_name(context_name)


def is_internal_task_machine_context_name(context_name: str | None) -> bool:
    """Return True when the name refers to an internal task machine context."""

    normalized = (context_name or "").strip("/")
    if normalized.startswith(HIVE_CONTEXT_PREFIX):
        return False
    if normalized in _INTERNAL_TASK_MACHINE_CONTEXT_NAMES:
        return True
    segments = _split_context_name(normalized)
    if len(segments) < 4:
        return False
    return "/".join(segments[-2:]) in _INTERNAL_TASK_MACHINE_CONTEXT_NAMES


def is_protected_task_surface_context_name(context_name: str | None) -> bool:
    """Return True for built-in task contexts that should not be removed."""

    normalized = (context_name or "").strip("/")
    if normalized.startswith(HIVE_CONTEXT_PREFIX):
        return False
    return is_task_surface_context_name(
        normalized,
    ) or is_internal_task_machine_context_name(
        normalized,
    )


def resolve_tasks_context_name(
    session: Session,
    project_id: int,
    *,
    assistant_id: str | None = None,
    source_task_log_id: int | None = None,
    tasks_context_name: str | None = None,
) -> str:
    """Resolve the assistant-scoped `.../Tasks` context for task-machine IO.

    Resolution prefers an explicit context name when one is already available,
    then falls back to the source task log id, and finally derives the path from
    assistant identity.
    """

    normalized_tasks_context_name = (tasks_context_name or "").strip("/")
    if is_task_surface_context_name(normalized_tasks_context_name):
        return normalized_tasks_context_name

    if source_task_log_id is not None:
        source_context_name = _get_task_surface_context_name_for_log_id(
            session=session,
            project_id=project_id,
            log_event_id=source_task_log_id,
        )
        if source_context_name is not None:
            return source_context_name

    derived_context_name = _derive_tasks_context_name_from_assistant(
        session=session,
        project_id=project_id,
        assistant_id=assistant_id,
    )
    if derived_context_name is not None:
        return derived_context_name

    raise ValueError(
        "Unable to resolve an assistant-scoped Tasks context for task-machine access.",
    )


def _get_task_surface_context_name_for_log_id(
    session: Session,
    *,
    project_id: int,
    log_event_id: int,
) -> str | None:
    """Return the task-surface context name for one task log when present."""

    context_names = (
        session.query(Context.name)
        .join(LogEventContext, LogEventContext.context_id == Context.id)
        .filter(
            Context.project_id == project_id,
            LogEventContext.log_event_id == log_event_id,
        )
        .all()
    )
    for (context_name,) in context_names:
        if is_task_surface_context_name(context_name):
            return str(context_name).strip("/")
    return None


def _derive_tasks_context_name_from_assistant(
    session: Session,
    *,
    project_id: int,
    assistant_id: str | None,
) -> str | None:
    """Return the canonical `.../Tasks` context for one assistant when resolvable."""

    normalized_assistant_id = _coerce_optional_str(assistant_id)
    if not normalized_assistant_id:
        return None

    assistant = _get_assistant_for_task_machine_lookup(
        session=session,
        assistant_id=normalized_assistant_id,
    )
    if assistant is not None and assistant.user_id:
        return _build_assistant_tasks_context_name(
            user_id=str(assistant.user_id),
            assistant_id=normalized_assistant_id,
        )

    candidate_context_names = [
        str(context_name).strip("/")
        for (context_name,) in session.query(Context.name)
        .filter(
            Context.project_id == project_id,
            Context.name.like(f"%/{normalized_assistant_id}/{TASKS_CONTEXT_NAME}"),
        )
        .all()
    ]
    matches = [
        context_name
        for context_name in candidate_context_names
        if is_task_surface_context_name(context_name)
        and _assistant_id_from_context_name(context_name) == normalized_assistant_id
    ]
    if not matches:
        return None
    if len(matches) > 1:
        raise ValueError(
            f"Ambiguous Tasks contexts found for assistant_id={normalized_assistant_id!r}.",
        )
    return matches[0]


def _get_assistant_for_task_machine_lookup(
    session: Session,
    *,
    assistant_id: str,
) -> Assistant | None:
    """Return the assistant row used to derive the owner-scoped Tasks path."""

    assistant_id_int = _coerce_int(assistant_id)
    if assistant_id_int is None:
        return None
    return session.execute(
        select(Assistant).where(Assistant.agent_id == assistant_id_int),
    ).scalar_one_or_none()


def _build_assistant_tasks_context_name(*, user_id: str, assistant_id: str) -> str:
    """Return the canonical assistant-scoped user Tasks context path."""

    return (
        f"{str(user_id).strip('/')}/{str(assistant_id).strip('/')}/{TASKS_CONTEXT_NAME}"
    )


_ACTIVATION_FIELD_DEFINITIONS: dict[str, dict[str, Any]] = {
    "assistant_id": {
        "field_type": "str",
        "mutable": False,
        "description": "Assistant identifier mirrored from the source task row.",
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
        "description": "Logical task identifier mirrored from the source task row.",
    },
    "source_task_log_id": {
        "field_type": "int",
        "mutable": True,
        "description": "Current task row that owns this activation.",
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
    "task_name": {
        "field_type": "str",
        "mutable": True,
        "description": "Current task title mirrored from the source task row.",
    },
    "task_description": {
        "field_type": "str",
        "mutable": True,
        "description": "Current task description mirrored from the source task row.",
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
        "field_type": "int",
        "mutable": True,
        "description": "Offline function_id when execution_mode=offline.",
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
        "description": "Updated timestamp from the source task row.",
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
        "description": "Task row that originated this run.",
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
    "source_medium": {
        "field_type": "str",
        "mutable": True,
        "description": "Inbound medium that triggered the run when applicable.",
    },
    "source_ref": {
        "field_type": "str",
        "mutable": True,
        "description": "Stable external reference for the triggering event or wake.",
    },
    "source_contact_id": {
        "field_type": "str",
        "mutable": True,
        "description": "Contact identifier associated with the triggering event.",
    },
    "source_contact_display_name": {
        "field_type": "str",
        "mutable": True,
        "description": "Human-readable contact name associated with the triggering event.",
    },
    "task_name": {
        "field_type": "str",
        "mutable": True,
        "description": "Human-readable task title mirrored into the run row.",
    },
    "task_description": {
        "field_type": "str",
        "mutable": True,
        "description": "Human-readable task description mirrored into the run row.",
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
    "job_name": {
        "field_type": "str",
        "mutable": True,
        "description": "Owning runtime job name for offline or live execution when known.",
    },
}


_OUTBOUND_OPERATION_FIELD_DEFINITIONS: dict[str, dict[str, Any]] = {
    "assistant_id": {
        "field_type": "str",
        "mutable": False,
        "description": "Assistant identifier that owns this outbound operation.",
    },
    "operation_id": {
        "field_type": "int",
        "mutable": False,
        "description": "Stable internal outbound-operation identifier.",
    },
    "operation_key": {
        "field_type": "str",
        "mutable": False,
        "unique": True,
        "description": "Idempotency key for one outbound communication attempt.",
    },
    "task_run_key": {
        "field_type": "str",
        "mutable": False,
        "description": "Owning task run key for the outbound attempt.",
    },
    "task_id": {
        "field_type": "int",
        "mutable": True,
        "description": "Logical task identifier associated with the outbound attempt.",
    },
    "source_task_log_id": {
        "field_type": "int",
        "mutable": True,
        "description": "Owning Unity/Tasks row for the outbound attempt when known.",
    },
    "operation_index": {
        "field_type": "int",
        "mutable": False,
        "description": "Monotonic ordinal within one task run for stable operation keys.",
    },
    "method_name": {
        "field_type": "str",
        "mutable": False,
        "description": "Comms primitive method used for the outbound attempt.",
    },
    "medium": {
        "field_type": "str",
        "mutable": False,
        "description": "Communication medium used by the outbound attempt.",
    },
    "target_kind": {
        "field_type": "str",
        "mutable": False,
        "description": "Target category such as contact, discord_channel, or email.",
    },
    "contact_id": {
        "field_type": "int",
        "mutable": True,
        "description": "Resolved contact identifier when the outbound is contact-anchored.",
    },
    "target_metadata": {
        "field_type": "dict",
        "mutable": True,
        "description": "Serialized destination details needed to understand the attempt.",
    },
    "status": {
        "field_type": "str",
        "mutable": True,
        "description": "Current ledger state for the outbound attempt.",
    },
    "provider_message_id": {
        "field_type": "str",
        "mutable": True,
        "description": "Provider-specific delivery identifier when available.",
    },
    "history_exchange_id": {
        "field_type": "int",
        "mutable": True,
        "description": "Transcript exchange id created for this outbound attempt.",
    },
    "history_message_id": {
        "field_type": "int",
        "mutable": True,
        "description": "Transcript message id created for this outbound attempt.",
    },
    "error": {
        "field_type": "str",
        "mutable": True,
        "description": "Hidden error payload for failed outbound attempts.",
    },
    "created_at": {
        "field_type": "datetime",
        "mutable": False,
        "description": "Creation timestamp for the outbound ledger row.",
    },
    "updated_at": {
        "field_type": "datetime",
        "mutable": True,
        "description": "Last update timestamp for the outbound ledger row.",
    },
    "completed_at": {
        "field_type": "datetime",
        "mutable": True,
        "description": "Completion timestamp for the outbound attempt.",
    },
}


def ensure_task_machine_contexts(
    session: Session,
    project_id: int,
    *,
    user_id: str,
    assistant_id: str,
) -> TaskMachineContextIds:
    """Ensure the per-body task machine contexts and schemas exist.

    Machine state (activations, runs, outbound operations) always lives under
    the owning body's ``{user_id}/{assistant_id}/Tasks/...`` tree, regardless
    of where the task definition was authored. Hive-scoped task definitions
    (``Hives/{hive_id}/Tasks``) route into this same per-body tree by passing
    the resolved owner explicitly — they never append ``/Activations`` under
    the Hive path.
    """

    context_names = _build_task_machine_context_names_for_owner(
        user_id=user_id,
        assistant_id=assistant_id,
    )

    activations_context_id = _upsert_context(
        session=session,
        project_id=project_id,
        name=context_names.activations_context_name,
        description="Internal machine-facing activation state for assistant tasks.",
        allow_duplicates=False,
        unique_keys={_TASK_ACTIVATION_UNIQUE_FIELD: "str"},
    )
    runs_context_id = _upsert_context(
        session=session,
        project_id=project_id,
        name=context_names.runs_context_name,
        description="Internal idempotent execution history for assistant tasks.",
        allow_duplicates=False,
        unique_keys={_TASK_RUN_UNIQUE_FIELD: "str"},
    )
    outbound_operations_context_id = _upsert_context(
        session=session,
        project_id=project_id,
        name=context_names.outbound_operations_context_name,
        description="Internal idempotent outbound communication ledger for assistant tasks.",
        allow_duplicates=False,
        unique_keys={_TASK_OUTBOUND_OPERATION_UNIQUE_FIELD: "str"},
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
    _upsert_field_types(
        session=session,
        project_id=project_id,
        context_id=outbound_operations_context_id,
        field_definitions=_OUTBOUND_OPERATION_FIELD_DEFINITIONS,
    )
    session.flush()
    return TaskMachineContextIds(
        activations_context_id=activations_context_id,
        runs_context_id=runs_context_id,
        outbound_operations_context_id=outbound_operations_context_id,
    )


def sync_task_activations_for_task_ids(
    session: Session,
    project_id: int,
    task_ids: Iterable[int],
    *,
    tasks_context_name: str = TASKS_CONTEXT_NAME,
) -> dict[str, int]:
    """Project one Tasks surface into each owning body's ``Tasks/Activations``.

    For the canonical per-body surface (``{user}/{assistant}/Tasks``) the
    batch projects into a single ``{user}/{assistant}/Tasks/Activations``
    context. For the Hive-shared definition surface
    (``Hives/{hive_id}/Tasks``), rows are bucketed by the ``_assistant_id``
    stamped on each row and each bucket projects into its own body's
    per-body machine-state tree. Batches that mix owners therefore fan out
    into one ``ensure_task_machine_contexts`` call per distinct body.
    """

    unique_task_ids = sorted({int(task_id) for task_id in task_ids})
    if not unique_task_ids or not is_task_surface_context_name(tasks_context_name):
        return {"upserted": 0, "deleted": 0}

    tasks_context_id = _get_context_id(
        session=session,
        project_id=project_id,
        name=tasks_context_name,
    )
    if tasks_context_id is None:
        return {"upserted": 0, "deleted": 0}

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

    owner_buckets = _bucket_task_ids_by_owning_body(
        session=session,
        unique_task_ids=unique_task_ids,
        rows_by_task_id=rows_by_task_id,
        tasks_context_name=tasks_context_name,
    )

    upserted = 0
    deleted = 0
    materialization_pairs: list[tuple[dict[str, Any] | None, dict[str, Any] | None]] = (
        []
    )
    for (user_id, assistant_id), bucket_task_ids in owner_buckets:
        context_ids = ensure_task_machine_contexts(
            session=session,
            project_id=project_id,
            user_id=user_id,
            assistant_id=assistant_id,
        )
        for task_id in bucket_task_ids:
            activation_key = _build_activation_key(
                assistant_id=assistant_id,
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


def _bucket_task_ids_by_owning_body(
    session: Session,
    *,
    unique_task_ids: Sequence[int],
    rows_by_task_id: Mapping[int, Sequence[_TaskRow]],
    tasks_context_name: str,
) -> list[tuple[tuple[str, str], list[int]]]:
    """Group a batch of task ids by the per-body owner of each row.

    Per-body Tasks surfaces produce a single bucket keyed off the path. Hive
    surfaces enumerate the ``_assistant_id`` on each row, look up the owning
    body's ``user_id`` in the Assistant table, and emit one bucket per
    distinct owner. Task ids without any rows in the batch are skipped on
    Hive surfaces because the owner is unknown; solo surfaces keep them in
    the single bucket so orphan activations can still be cleared.
    """

    if not tasks_context_name.startswith(HIVE_CONTEXT_PREFIX):
        user_id, assistant_id = _split_per_body_tasks_context_name(tasks_context_name)
        return [((user_id, assistant_id), list(unique_task_ids))]

    task_ids_by_assistant: dict[str, list[int]] = {}
    for task_id in unique_task_ids:
        rows = list(rows_by_task_id.get(task_id, []))
        if not rows:
            continue
        assistant_id = _resolve_assistant_id(
            task_row=rows[0],
            tasks_context_name=tasks_context_name,
        )
        if assistant_id is None:
            raise ValueError(
                f"Hive task row missing _assistant_id: log_event_id={rows[0].log_event_id}",
            )
        task_ids_by_assistant.setdefault(assistant_id, []).append(task_id)

    ordered_buckets: list[tuple[tuple[str, str], list[int]]] = []
    for assistant_id in sorted(task_ids_by_assistant):
        assistant = _get_assistant_for_task_machine_lookup(
            session=session,
            assistant_id=assistant_id,
        )
        if assistant is None or not assistant.user_id:
            raise ValueError(
                "Hive task row references unknown assistant: "
                f"assistant_id={assistant_id!r}.",
            )
        ordered_buckets.append(
            (
                (str(assistant.user_id), assistant_id),
                task_ids_by_assistant[assistant_id],
            ),
        )
    return ordered_buckets


def get_task_activation(
    session: Session,
    project_id: int,
    *,
    assistant_id: str | None,
    task_id: int,
) -> LogEvent | None:
    """Return the current activation row for one assistant/task pair, if present."""

    tasks_context_name = resolve_tasks_context_name(
        session=session,
        project_id=project_id,
        assistant_id=assistant_id,
    )
    user_id, resolved_assistant_id = _split_per_body_tasks_context_name(
        tasks_context_name,
    )
    context_ids = ensure_task_machine_contexts(
        session=session,
        project_id=project_id,
        user_id=user_id,
        assistant_id=resolved_assistant_id,
    )
    activation_key = _build_activation_key(
        assistant_id=assistant_id,
        task_id=task_id,
    )
    activation = _get_machine_row_by_unique_field(
        session=session,
        context_id=context_ids.activations_context_id,
        unique_field_name=_TASK_ACTIVATION_UNIQUE_FIELD,
        unique_field_value=activation_key,
    )
    if activation is not None:
        return activation
    return _migrate_legacy_machine_row_if_present(
        session=session,
        project_id=project_id,
        legacy_context_name=TASK_ACTIVATIONS_CONTEXT_NAME,
        nested_context_id=context_ids.activations_context_id,
        unique_field_name=_TASK_ACTIVATION_UNIQUE_FIELD,
        unique_field_value=activation_key,
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

    tasks_context_name = resolve_tasks_context_name(
        session=session,
        project_id=project_id,
        assistant_id=_coerce_optional_str(payload.get("assistant_id")),
        source_task_log_id=_coerce_int(payload.get("source_task_log_id")),
    )
    user_id, resolved_assistant_id = _split_per_body_tasks_context_name(
        tasks_context_name,
    )
    context_ids = ensure_task_machine_contexts(
        session=session,
        project_id=project_id,
        user_id=user_id,
        assistant_id=resolved_assistant_id,
    )
    existing = _get_machine_row_by_unique_field(
        session=session,
        context_id=context_ids.runs_context_id,
        unique_field_name=_TASK_RUN_UNIQUE_FIELD,
        unique_field_value=run_key,
    )
    if existing is not None:
        return existing, False
    migrated = _migrate_legacy_machine_row_if_present(
        session=session,
        project_id=project_id,
        legacy_context_name=TASK_RUNS_CONTEXT_NAME,
        nested_context_id=context_ids.runs_context_id,
        unique_field_name=_TASK_RUN_UNIQUE_FIELD,
        unique_field_value=run_key,
    )
    if migrated is not None:
        return migrated, False

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
    created_row = created.row
    if created_row.data.get("run_id") != created_row.id:
        created_payload = dict(created_row.data or {})
        created_payload["run_id"] = created_row.id
        _replace_log_payload(created_row, created_payload)
    session.flush()
    return created_row, created.created


def update_task_run(
    session: Session,
    project_id: int,
    assistant_id: str | None,
    run_key: str,
    updates: Mapping[str, Any],
) -> LogEvent:
    """Apply a partial update to an existing task run row."""

    tasks_context_name = resolve_tasks_context_name(
        session=session,
        project_id=project_id,
        assistant_id=assistant_id,
    )
    user_id, resolved_assistant_id = _split_per_body_tasks_context_name(
        tasks_context_name,
    )
    context_ids = ensure_task_machine_contexts(
        session=session,
        project_id=project_id,
        user_id=user_id,
        assistant_id=resolved_assistant_id,
    )
    existing = _get_machine_row_by_unique_field(
        session=session,
        context_id=context_ids.runs_context_id,
        unique_field_name=_TASK_RUN_UNIQUE_FIELD,
        unique_field_value=run_key,
    )
    if existing is None:
        existing = _migrate_legacy_machine_row_if_present(
            session=session,
            project_id=project_id,
            legacy_context_name=TASK_RUNS_CONTEXT_NAME,
            nested_context_id=context_ids.runs_context_id,
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
    *,
    assistant_id: str | None = None,
    source_task_log_id: int | None = None,
    tasks_context_name: str | None = None,
) -> LogEvent | None:
    """Return an existing task run row by run_key."""

    resolved_tasks_context_name = resolve_tasks_context_name(
        session=session,
        project_id=project_id,
        assistant_id=assistant_id,
        source_task_log_id=source_task_log_id,
        tasks_context_name=tasks_context_name,
    )
    user_id, resolved_assistant_id = _split_per_body_tasks_context_name(
        resolved_tasks_context_name,
    )
    context_ids = ensure_task_machine_contexts(
        session=session,
        project_id=project_id,
        user_id=user_id,
        assistant_id=resolved_assistant_id,
    )
    existing = _get_machine_row_by_unique_field(
        session=session,
        context_id=context_ids.runs_context_id,
        unique_field_name=_TASK_RUN_UNIQUE_FIELD,
        unique_field_value=run_key,
    )
    if existing is not None:
        return existing
    return _migrate_legacy_machine_row_if_present(
        session=session,
        project_id=project_id,
        legacy_context_name=TASK_RUNS_CONTEXT_NAME,
        nested_context_id=context_ids.runs_context_id,
        unique_field_name=_TASK_RUN_UNIQUE_FIELD,
        unique_field_value=run_key,
    )


def create_task_outbound_operation_if_absent(
    session: Session,
    project_id: int,
    payload: Mapping[str, Any],
) -> tuple[LogEvent, bool]:
    """Create an outbound operation row by `operation_key` if absent."""

    operation_key = payload.get("operation_key")
    if not isinstance(operation_key, str) or not operation_key:
        raise ValueError(
            "Outbound operation payload must include a non-empty operation_key.",
        )

    tasks_context_name = resolve_tasks_context_name(
        session=session,
        project_id=project_id,
        assistant_id=_coerce_optional_str(payload.get("assistant_id")),
        source_task_log_id=_coerce_int(payload.get("source_task_log_id")),
    )
    user_id, resolved_assistant_id = _split_per_body_tasks_context_name(
        tasks_context_name,
    )
    context_ids = ensure_task_machine_contexts(
        session=session,
        project_id=project_id,
        user_id=user_id,
        assistant_id=resolved_assistant_id,
    )
    existing = _get_machine_row_by_unique_field(
        session=session,
        context_id=context_ids.outbound_operations_context_id,
        unique_field_name=_TASK_OUTBOUND_OPERATION_UNIQUE_FIELD,
        unique_field_value=operation_key,
    )
    if existing is not None:
        return existing, False
    migrated = _migrate_legacy_machine_row_if_present(
        session=session,
        project_id=project_id,
        legacy_context_name=TASK_OUTBOUND_OPERATIONS_CONTEXT_NAME,
        nested_context_id=context_ids.outbound_operations_context_id,
        unique_field_name=_TASK_OUTBOUND_OPERATION_UNIQUE_FIELD,
        unique_field_value=operation_key,
    )
    if migrated is not None:
        return migrated, False

    materialized_payload = dict(payload)
    materialized_payload.setdefault("status", "pending")
    created = _upsert_machine_row(
        session=session,
        project_id=project_id,
        context_id=context_ids.outbound_operations_context_id,
        unique_field_name=_TASK_OUTBOUND_OPERATION_UNIQUE_FIELD,
        unique_field_value=operation_key,
        payload=materialized_payload,
    )
    created_row = created.row
    if created_row.data.get("operation_id") != created_row.id:
        created_payload = dict(created_row.data or {})
        created_payload["operation_id"] = created_row.id
        _replace_log_payload(created_row, created_payload)
    session.flush()
    return created_row, created.created


def update_task_outbound_operation(
    session: Session,
    project_id: int,
    assistant_id: str | None,
    operation_key: str,
    updates: Mapping[str, Any],
) -> LogEvent:
    """Apply a partial update to an existing outbound operation row."""

    tasks_context_name = resolve_tasks_context_name(
        session=session,
        project_id=project_id,
        assistant_id=assistant_id,
    )
    user_id, resolved_assistant_id = _split_per_body_tasks_context_name(
        tasks_context_name,
    )
    context_ids = ensure_task_machine_contexts(
        session=session,
        project_id=project_id,
        user_id=user_id,
        assistant_id=resolved_assistant_id,
    )
    existing = _get_machine_row_by_unique_field(
        session=session,
        context_id=context_ids.outbound_operations_context_id,
        unique_field_name=_TASK_OUTBOUND_OPERATION_UNIQUE_FIELD,
        unique_field_value=operation_key,
    )
    if existing is None:
        existing = _migrate_legacy_machine_row_if_present(
            session=session,
            project_id=project_id,
            legacy_context_name=TASK_OUTBOUND_OPERATIONS_CONTEXT_NAME,
            nested_context_id=context_ids.outbound_operations_context_id,
            unique_field_name=_TASK_OUTBOUND_OPERATION_UNIQUE_FIELD,
            unique_field_value=operation_key,
        )
    if existing is None:
        raise ValueError(
            f"Outbound operation with operation_key='{operation_key}' not found.",
        )

    payload = dict(existing.data or {})
    for field_name, value in dict(updates).items():
        definition = _OUTBOUND_OPERATION_FIELD_DEFINITIONS.get(field_name)
        if definition is None:
            raise ValueError(
                f"Unknown outbound operation field '{field_name}' cannot be updated.",
            )
        if not definition.get("mutable", True) and payload.get(field_name) != value:
            raise ValueError(
                f"Outbound operation field '{field_name}' is immutable and cannot be changed.",
            )
    payload.update(dict(updates))
    payload.setdefault("operation_id", existing.id)
    _replace_log_payload(existing, payload)
    session.flush()
    return existing


def get_task_ids_for_log_ids(
    session: Session,
    project_id: int,
    *,
    context_name: str,
    log_event_ids: Iterable[int],
) -> set[int]:
    """Return logical task ids for the specified task rows."""

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
    execution_mode = "offline" if _coerce_bool(row.data.get("offline")) else "live"
    entrypoint = _coerce_int(row.data.get("entrypoint"))
    if execution_mode == "offline" and entrypoint is None:
        raise ValueError("Offline tasks require an integer entrypoint.")
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
        "execution_mode": execution_mode,
        "status": row.data.get("status"),
        "task_name": _coerce_optional_str(row.data.get("name")),
        "task_description": _coerce_optional_str(row.data.get("description")),
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
        "entrypoint": entrypoint,
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
            current_upsert_body["previous_execution_mode"] = previous_delete_body[
                "execution_mode"
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


def _scheduled_activation_snapshot(
    activation: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Return the shared scheduled-activation fields Communication expects."""

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
        "execution_mode": _coerce_optional_str(activation.get("execution_mode"))
        or "live",
    }


def _compact_task_summary(text: Any, *, fallback: str) -> str:
    """Return one compact wake-summary line for scheduled task delivery."""

    candidate = " ".join((_coerce_optional_str(text) or "").split())
    if not candidate:
        candidate = " ".join(fallback.split())
    if len(candidate) <= _TASK_SUMMARY_MAX_CHARS:
        return candidate
    truncated = candidate[: _TASK_SUMMARY_MAX_CHARS - 3].rstrip(" ,.;:")
    return f"{truncated}..."


def _scheduled_activation_wake_context(
    activation: Mapping[str, Any],
) -> dict[str, str]:
    """Return the compact human-facing wake context for one scheduled activation."""

    task_id = _coerce_int(activation.get("task_id"))
    task_label = _coerce_optional_str(activation.get("task_name")) or (
        f"task {task_id}" if task_id is not None else "scheduled task"
    )
    repeat = _coerce_optional_list(activation.get("repeat")) or []
    recurrence_hint = _RECURRING_WAKE_HINT if repeat else _ONE_OFF_WAKE_HINT
    return {
        "task_label": task_label,
        "task_summary": _compact_task_summary(
            activation.get("task_description"),
            fallback=task_label,
        ),
        "visibility_policy": _DEFAULT_SCHEDULED_TASK_VISIBILITY_POLICY,
        "recurrence_hint": recurrence_hint,
    }


def _scheduled_activation_upsert_body(
    activation: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Build the Communication upsert payload for one scheduled activation."""

    snapshot = _scheduled_activation_snapshot(activation)
    if snapshot is None:
        return None
    source_task_log_id = _coerce_int(activation.get("source_task_log_id"))
    if source_task_log_id is None:
        return None
    return {
        **snapshot,
        "source_task_log_id": source_task_log_id,
        "source_type": "scheduled",
        **_scheduled_activation_wake_context(activation),
    }


def _scheduled_activation_delete_body(
    activation: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Build the Communication delete payload for one scheduled activation."""

    return _scheduled_activation_snapshot(activation)


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
    """Load task rows for the given logical task ids."""

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
) -> _MachineRowUpsertResult:
    """Create or replace an internal machine row using a logical unique key."""

    existing = _get_machine_row_by_unique_field(
        session=session,
        context_id=context_id,
        unique_field_name=unique_field_name,
        unique_field_value=unique_field_value,
    )
    if existing is not None:
        _replace_log_payload(existing, payload)
        return _MachineRowUpsertResult(row=existing, created=False)

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
        return _MachineRowUpsertResult(row=log_event, created=True)

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
    return _MachineRowUpsertResult(row=existing, created=False)


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


def _migrate_legacy_machine_row_if_present(
    session: Session,
    *,
    project_id: int,
    legacy_context_name: str,
    nested_context_id: int,
    unique_field_name: str,
    unique_field_value: int | str,
) -> LogEvent | None:
    """Move one legacy global machine row into the assistant-scoped context."""

    legacy_context_id = _get_context_id(
        session=session,
        project_id=project_id,
        name=legacy_context_name,
    )
    if legacy_context_id is None:
        return None
    legacy_row = _get_machine_row_by_unique_field(
        session=session,
        context_id=legacy_context_id,
        unique_field_name=unique_field_name,
        unique_field_value=unique_field_value,
    )
    if legacy_row is None:
        return None
    migrated_row = _upsert_machine_row(
        session=session,
        project_id=project_id,
        context_id=nested_context_id,
        unique_field_name=unique_field_name,
        unique_field_value=unique_field_value,
        payload=dict(legacy_row.data or {}),
    )
    _delete_machine_row_by_unique_field(
        session=session,
        project_id=project_id,
        context_id=legacy_context_id,
        unique_field_name=unique_field_name,
        unique_field_value=unique_field_value,
    )
    return migrated_row.row


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


def _coerce_bool(value: Any) -> bool:
    """Best-effort boolean coercion for JSON-backed task rows."""

    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off", ""}:
            return False
    return bool(value)


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
