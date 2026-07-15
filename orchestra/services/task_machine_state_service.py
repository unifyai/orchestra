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

from orchestra.db.context_naming import TEAM_CONTEXT_PREFIX, is_team_context_name
from orchestra.db.dao.context_dao import delete_orphaned_log_events
from orchestra.db.dao.unique_constraint_dao import UniqueConstraintDAO
from orchestra.db.log_queries import log_event_context_join, owner_scope_clause
from orchestra.db.models.orchestra_models import (
    TEAM_STATUS_ACTIVE,
    Assistant,
    Context,
    FieldType,
    LogEvent,
    LogEventContext,
    LogUniqueConstraint,
    Team,
    TeamAssistantMembership,
)
from orchestra.db.scope import single_owner_key_for_context
from orchestra.provider_triggers.activation_revision import (
    compute_provider_event_activation_revision,
    normalize_provider_event_filters,
)
from orchestra.provider_triggers.task_trigger import parse_task_trigger
from orchestra.settings import settings

TASK_MACHINE_PROJECT_NAME = "Assistants"
TASKS_CONTEXT_NAME = "Tasks"
TASK_ACTIVATIONS_CONTEXT_NAME = "Tasks/Activations"
TASK_RUNS_CONTEXT_NAME = "Tasks/Runs"
TASK_OUTBOUND_OPERATIONS_CONTEXT_NAME = "Tasks/OutboundOperations"
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

_SCHEDULED_ACTIVATION_STATUSES = {"scheduled"}
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


@dataclass(frozen=True)
class _TaskProjectionGroup:
    """Rows that project into one executor-owned task activation."""

    assistant_id: str | None
    task_id: int
    rows: list[_TaskRow]


def _split_context_name(context_name: str | None) -> list[str]:
    """Return non-empty path segments for a context name."""

    return [
        segment for segment in (context_name or "").strip("/").split("/") if segment
    ]


def _assistant_id_from_context_name(context_name: str | None) -> str | None:
    """Extract the assistant id from an assistant-scoped tasks context."""

    segments = _split_context_name(context_name)
    if len(segments) < 2 or segments[-1] != TASKS_CONTEXT_NAME:
        return None
    if is_team_context_name(context_name):
        return None
    if segments[-2] == _ALL_CONTEXT_SEGMENT:
        return None
    return segments[-2]


def _team_id_from_context_name(context_name: str | None) -> int | None:
    """Extract the team id from a shared-team task surface context."""

    segments = _split_context_name(context_name)
    if len(segments) != 3 or segments[-1] != TASKS_CONTEXT_NAME:
        return None
    if segments[0] != "Teams":
        return None
    return _coerce_int(segments[1])


def _destination_from_context_name(context_name: str | None) -> str | None:
    """Return the public destination label represented by a task surface path."""

    team_id = _team_id_from_context_name(context_name)
    if team_id is None:
        return None
    return f"team:{team_id}"


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


def _resolve_assistant_id(
    *,
    task_row: _TaskRow | None = None,
    task_data: Mapping[str, Any] | None = None,
    tasks_context_name: str | None = None,
) -> str | None:
    """Resolve assistant ownership from row data first, then the context path."""

    candidate_data = task_row.data if task_row is not None else task_data
    if isinstance(candidate_data, Mapping):
        assistant_id = _coerce_optional_str(candidate_data.get("assistant_id"))
        if assistant_id:
            return assistant_id
        assistant_id = _coerce_optional_str(candidate_data.get("_assistant_id"))
        if assistant_id:
            return assistant_id
    return _assistant_id_from_context_name(tasks_context_name)


def _build_activation_key(
    *,
    assistant_id: str | None,
    task_id: int,
    destination: str | None = None,
) -> str:
    """Return the executor-scoped activation key used for uniqueness."""

    destination_label = _coerce_optional_str(destination)
    if assistant_id:
        if destination_label:
            return f"{assistant_id}:{destination_label}:{task_id}"
        return f"{assistant_id}:{task_id}"
    if destination_label:
        return f"{destination_label}:{task_id}"
    return str(task_id)


def is_task_surface_context_name(context_name: str | None) -> bool:
    """Return True when the name refers to the user-authored tasks table."""

    segments = _split_context_name(context_name)
    if not segments or segments[-1] != TASKS_CONTEXT_NAME:
        return False
    if is_team_context_name(context_name):
        return (
            len(segments) == 3 and _team_id_from_context_name(context_name) is not None
        )
    if len(segments) >= 2 and segments[-2] == _ALL_CONTEXT_SEGMENT:
        return False
    return not is_internal_task_machine_context_name(context_name)


def is_internal_task_machine_context_name(context_name: str | None) -> bool:
    """Return True when the name refers to an internal task machine context."""

    normalized = (context_name or "").strip("/")
    if normalized in _INTERNAL_TASK_MACHINE_CONTEXT_NAMES:
        return True
    segments = _split_context_name(normalized)
    if len(segments) < 4:
        return False
    return "/".join(segments[-2:]) in _INTERNAL_TASK_MACHINE_CONTEXT_NAMES


def is_protected_task_surface_context_name(context_name: str | None) -> bool:
    """Return True for built-in task contexts that should not be removed."""

    normalized = (context_name or "").strip("/")
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
            LogEventContext.project_id == project_id,
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


def _executor_tasks_context_name(
    session: Session,
    *,
    project_id: int,
    assistant_id: str | None,
    source_tasks_context_name: str,
) -> str | None:
    """Return the assistant-owned Tasks context for projected machine state."""

    normalized_assistant_id = _coerce_optional_str(assistant_id)
    if normalized_assistant_id:
        return _derive_tasks_context_name_from_assistant(
            session=session,
            project_id=project_id,
            assistant_id=normalized_assistant_id,
        )
    if not is_team_context_name(source_tasks_context_name):
        return (source_tasks_context_name or "").strip("/")
    return None


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


def _assistant_is_team_member(
    session: Session,
    *,
    assistant_id: str | None,
    team_id: int | None,
) -> bool:
    """Return whether an assistant currently belongs to a shared team."""

    assistant_id_int = _coerce_int(assistant_id)
    if assistant_id_int is None or team_id is None:
        return False
    return (
        session.execute(
            select(TeamAssistantMembership)
            .join(Team, Team.id == TeamAssistantMembership.team_id)
            .where(
                TeamAssistantMembership.assistant_id == assistant_id_int,
                TeamAssistantMembership.team_id == team_id,
                Team.status == TEAM_STATUS_ACTIVE,
            ),
        ).scalar_one_or_none()
        is not None
    )


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
    "destination": {
        "field_type": "str",
        "mutable": True,
        "description": "Team destination for the source task definition.",
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
        "description": "How the task wakes: scheduled, triggered, or provider_event.",
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
        "description": "Whether a trigger activation interrupts the assistant.",
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
    "max_runtime_seconds": {
        "field_type": "int",
        "mutable": True,
        "description": (
            "Optional per-task execution bound mirrored from the source task "
            "row; null means the run is unbounded."
        ),
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
    "provider_event_binding_id": {
        "field_type": "str",
        "mutable": True,
        "description": "Stable binding identifier for provider-event activations.",
    },
    "acceptance_epoch": {
        "field_type": "int",
        "mutable": True,
        "description": "Lifecycle fence for provider-event acceptance ordering.",
    },
    "connection_id": {
        "field_type": "str",
        "mutable": True,
        "description": "Pinned integration connection for provider-event matching.",
    },
    "backend_id": {
        "field_type": "str",
        "mutable": True,
        "description": "Provider backend for provider-event triggers.",
    },
    "canonical_app_slug": {
        "field_type": "str",
        "mutable": True,
        "description": "Canonical app slug for provider-event triggers.",
    },
    "event_slug": {
        "field_type": "str",
        "mutable": True,
        "description": "Canonical event slug for provider-event triggers.",
    },
    "schema_version": {
        "field_type": "str",
        "mutable": True,
        "description": "Registry schema version for provider-event triggers.",
    },
    "provider_event_filters": {
        "field_type": "list",
        "mutable": True,
        "description": "Normalized AND filters for provider-event matching.",
    },
    "provider_account_subject_hmac": {
        "field_type": "str",
        "mutable": True,
        "description": "Pinned provider account subject digest when known.",
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
    "destination": {
        "field_type": "str",
        "mutable": True,
        "description": "Team destination for the source task definition.",
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
    "provider_event_binding_id": {
        "field_type": "str",
        "mutable": True,
        "description": "Stable binding identifier for a provider-event run.",
    },
    "provider_event_receipt_id": {
        "field_type": "str",
        "mutable": True,
        "description": "Durable receipt identifier accepted for this provider-event run.",
    },
    "provider_event_backend_id": {
        "field_type": "str",
        "mutable": True,
        "description": "Trigger backend that delivered the accepted provider event.",
    },
    "provider_event_app_slug": {
        "field_type": "str",
        "mutable": True,
        "description": "Canonical app slug for the accepted provider event.",
    },
    "provider_event_slug": {
        "field_type": "str",
        "mutable": True,
        "description": "Canonical event slug for the accepted provider event.",
    },
    "provider_event_schema_version": {
        "field_type": "str",
        "mutable": True,
        "description": "Registry schema version used when the event was accepted.",
    },
    "provider_event_acceptance_epoch": {
        "field_type": "int",
        "mutable": True,
        "description": "Acceptance epoch captured when the provider event was accepted.",
    },
    "provider_event_received_at": {
        "field_type": "datetime",
        "mutable": True,
        "description": "When Orchestra durably accepted the provider event.",
    },
    "provider_event_occurred_at": {
        "field_type": "datetime",
        "mutable": True,
        "description": "Provider-reported event occurrence time when available.",
    },
    "provider_event_matched_filters": {
        "field_type": "list",
        "mutable": True,
        "description": "Filter snapshot that matched when the provider event was accepted.",
    },
    "provider_event_identity_hmac": {
        "field_type": "str",
        "mutable": True,
        "description": "Per-binding HMAC digest of the retry-stable provider event identity.",
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
    tasks_context_name: str,
) -> TaskMachineContextIds:
    """Ensure the assistant-scoped task machine contexts and schemas exist."""

    context_names = _resolve_task_machine_context_names(tasks_context_name)

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
    """Project one assistant-scoped tasks table into `Tasks/Activations`."""

    unique_task_ids = sorted({int(task_id) for task_id in task_ids})
    if not unique_task_ids or not is_task_surface_context_name(tasks_context_name):
        return {"upserted": 0, "deleted": 0}
    normalized_tasks_context_name = (tasks_context_name or "").strip("/")
    source_destination = _destination_from_context_name(normalized_tasks_context_name)
    source_team_id = _team_id_from_context_name(normalized_tasks_context_name)

    tasks_context_id = _get_context_id(
        session=session,
        project_id=project_id,
        name=normalized_tasks_context_name,
    )
    if tasks_context_id is None:
        return {"upserted": 0, "deleted": 0}
    task_rows = _load_task_rows(
        session=session,
        project_id=project_id,
        context_id=tasks_context_id,
        task_ids=unique_task_ids,
    )
    projection_groups = _projection_groups_for_task_rows(
        task_rows,
        task_ids=unique_task_ids,
        tasks_context_name=normalized_tasks_context_name,
    )

    upserted = 0
    deleted = 0
    materialization_pairs: list[tuple[dict[str, Any] | None, dict[str, Any] | None]] = (
        []
    )
    handled_task_ids = {group.task_id for group in projection_groups}
    for task_id in unique_task_ids:
        if task_id in handled_task_ids:
            continue
        if source_destination is not None:
            deleted_rows = _delete_activation_rows_by_task_destination(
                session=session,
                project_id=project_id,
                task_id=task_id,
                destination=source_destination,
            )
            deleted += len(deleted_rows)
            materialization_pairs.extend((row, None) for row in deleted_rows)
            continue
        assistant_id = _assistant_id_from_context_name(normalized_tasks_context_name)
        projection_groups.append(
            _TaskProjectionGroup(
                assistant_id=assistant_id,
                task_id=task_id,
                rows=[],
            ),
        )

    for group in projection_groups:
        executor_tasks_context_name = _executor_tasks_context_name(
            session=session,
            project_id=project_id,
            assistant_id=group.assistant_id,
            source_tasks_context_name=normalized_tasks_context_name,
        )
        if executor_tasks_context_name is None:
            if source_destination is not None:
                deleted_rows = _delete_activation_rows_by_task_destination(
                    session=session,
                    project_id=project_id,
                    task_id=group.task_id,
                    destination=source_destination,
                )
                deleted += len(deleted_rows)
                materialization_pairs.extend((row, None) for row in deleted_rows)
            continue
        context_ids = ensure_task_machine_contexts(
            session=session,
            project_id=project_id,
            tasks_context_name=executor_tasks_context_name,
        )
        activation_key = _build_activation_key(
            assistant_id=group.assistant_id,
            task_id=group.task_id,
            destination=source_destination,
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
        if source_destination is not None and not _assistant_is_team_member(
            session=session,
            assistant_id=group.assistant_id,
            team_id=source_team_id,
        ):
            activation_payload = None
        else:
            activation_payload = _build_activation_payload(
                rows=group.rows,
                tasks_context_name=normalized_tasks_context_name,
                destination=source_destination,
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


def lookup_task_machine_activation_context_id(
    session: Session,
    project_id: int,
    *,
    tasks_context_name: str,
) -> int | None:
    """Return the activations context id when it already exists (read-only)."""

    context_names = _resolve_task_machine_context_names(tasks_context_name)
    return _get_context_id(
        session=session,
        project_id=project_id,
        name=context_names.activations_context_name,
    )


def get_task_activation(
    session: Session,
    project_id: int,
    *,
    assistant_id: str | None,
    task_id: int,
    destination: str | None = None,
) -> LogEvent | None:
    """Return the current activation row for one assistant/task pair, if present.

    Lookup is read-only: it never creates machine contexts or upserts field
    types. Schema materialization belongs on write/projection paths via
    ``ensure_task_machine_contexts``.
    """

    tasks_context_name = resolve_tasks_context_name(
        session=session,
        project_id=project_id,
        assistant_id=assistant_id,
    )
    activation_key = _build_activation_key(
        assistant_id=assistant_id,
        task_id=task_id,
        destination=destination,
    )
    activations_context_id = lookup_task_machine_activation_context_id(
        session=session,
        project_id=project_id,
        tasks_context_name=tasks_context_name,
    )
    if activations_context_id is not None:
        activation = _get_machine_row_by_unique_field(
            session=session,
            context_id=activations_context_id,
            unique_field_name=_TASK_ACTIVATION_UNIQUE_FIELD,
            unique_field_value=activation_key,
        )
        if activation is not None:
            return activation

    # Read-only legacy fallback — do not migrate on lookup.
    legacy_context_id = _get_context_id(
        session=session,
        project_id=project_id,
        name=TASK_ACTIVATIONS_CONTEXT_NAME,
    )
    if legacy_context_id is None:
        return None
    return _get_machine_row_by_unique_field(
        session=session,
        context_id=legacy_context_id,
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
    context_ids = ensure_task_machine_contexts(
        session=session,
        project_id=project_id,
        tasks_context_name=tasks_context_name,
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
    source_task_log_id: int | None = None,
) -> LogEvent:
    """Apply a partial update to an existing task run row.

    Resolution mirrors :func:`create_task_run_if_absent`: the run row lives
    under its task's own surface (a team task's runs live in
    ``Teams/{id}/Tasks/Runs``), so ``source_task_log_id`` takes precedence
    over assistant derivation. Updates that arrive without it (older
    runtimes) fall back to locating the row by its globally-unique
    ``run_key`` across team task surfaces.
    """

    tasks_context_name = resolve_tasks_context_name(
        session=session,
        project_id=project_id,
        assistant_id=assistant_id,
        source_task_log_id=source_task_log_id,
    )
    context_ids = ensure_task_machine_contexts(
        session=session,
        project_id=project_id,
        tasks_context_name=tasks_context_name,
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
        existing = _find_machine_row_in_team_surfaces(
            session=session,
            project_id=project_id,
            leaf_name=_TASK_RUNS_CONTEXT_LEAF,
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
    context_ids = ensure_task_machine_contexts(
        session=session,
        project_id=project_id,
        tasks_context_name=resolved_tasks_context_name,
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


def get_task_run_by_run_id(
    session: Session,
    project_id: int,
    *,
    run_id: int,
) -> LogEvent | None:
    """Return one task run row by its stable ``run_id`` (the run's log_event id).

    Runs live under an assistant-scoped ``.../Tasks/Runs`` context and their
    ``run_id`` equals the backing ``LogEvent.id``. Callers pass the run id
    received out-of-band (for example on a provider-event dispatch) and get the
    row back only when it is genuinely a task-run row in the given project.
    """

    return (
        session.query(LogEvent)
        .join(LogEventContext, log_event_context_join())
        .join(Context, Context.id == LogEventContext.context_id)
        .filter(
            LogEvent.project_id == project_id,
            LogEventContext.project_id == project_id,
            Context.project_id == project_id,
            LogEvent.id == run_id,
            Context.name.like(f"%{TASK_RUNS_CONTEXT_NAME}"),
        )
        .first()
    )


def get_latest_task_run_for_task(
    session: Session,
    project_id: int,
    *,
    assistant_id: str,
    task_id: int,
    source_task_log_id: int | None = None,
    tasks_context_name: str | None = None,
) -> LogEvent | None:
    """Return the most recently updated run row for one assistant/task pair."""

    resolved_tasks_context_name = resolve_tasks_context_name(
        session=session,
        project_id=project_id,
        assistant_id=assistant_id,
        source_task_log_id=source_task_log_id,
        tasks_context_name=tasks_context_name,
    )
    context_ids = ensure_task_machine_contexts(
        session=session,
        project_id=project_id,
        tasks_context_name=resolved_tasks_context_name,
    )
    owner_key_filter = single_owner_key_for_context(
        session,
        context_ids.runs_context_id,
    )
    filters = [
        LogEvent.project_id == project_id,
        LogEventContext.project_id == project_id,
        owner_scope_clause(LogEvent, owner_key_filter),
        owner_scope_clause(LogEventContext, owner_key_filter),
        LogEventContext.context_id == context_ids.runs_context_id,
        LogEvent.data.has_key("assistant_id"),
        LogEvent.data.has_key("task_id"),
        LogEvent.data.op("->>")("assistant_id") == str(assistant_id),
        LogEvent.data.op("->>")("task_id") == str(task_id),
    ]
    if source_task_log_id is not None:
        filters.extend(
            [
                LogEvent.data.has_key("source_task_log_id"),
                LogEvent.data.op("->>")("source_task_log_id")
                == str(source_task_log_id),
            ],
        )
    return (
        session.query(LogEvent)
        .join(LogEventContext, log_event_context_join(owner_key=owner_key_filter))
        .filter(*filters)
        .order_by(LogEvent.updated_at.desc(), LogEvent.created_at.desc())
        .first()
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
    context_ids = ensure_task_machine_contexts(
        session=session,
        project_id=project_id,
        tasks_context_name=tasks_context_name,
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
    source_task_log_id: int | None = None,
) -> LogEvent:
    """Apply a partial update to an existing outbound operation row.

    Resolution mirrors :func:`create_task_outbound_operation_if_absent`:
    ``source_task_log_id`` takes precedence so team-task rows resolve to the
    team surface, with a key-based fallback for updates that arrive without
    it (older runtimes).
    """

    tasks_context_name = resolve_tasks_context_name(
        session=session,
        project_id=project_id,
        assistant_id=assistant_id,
        source_task_log_id=source_task_log_id,
    )
    context_ids = ensure_task_machine_contexts(
        session=session,
        project_id=project_id,
        tasks_context_name=tasks_context_name,
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
        existing = _find_machine_row_in_team_surfaces(
            session=session,
            project_id=project_id,
            leaf_name=_TASK_OUTBOUND_OPERATIONS_CONTEXT_LEAF,
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

    owner_key_filter = single_owner_key_for_context(session, context_id)
    rows = (
        session.query(LogEvent.data)
        .join(LogEventContext, log_event_context_join(owner_key=owner_key_filter))
        .filter(
            LogEvent.project_id == project_id,
            LogEventContext.project_id == project_id,
            owner_scope_clause(LogEvent, owner_key_filter),
            owner_scope_clause(LogEventContext, owner_key_filter),
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
    destination: str | None,
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
            destination=destination,
        )

    provider_candidates = [
        row for row in ordered_rows if _is_provider_event_activation_candidate(row.data)
    ]
    if provider_candidates:
        return _project_provider_event_activation_payload(
            row=provider_candidates[0],
            tasks_context_name=tasks_context_name,
            destination=destination,
        )

    trigger_candidates = [
        row
        for row in ordered_rows
        if _is_communication_trigger_activation_candidate(row.data)
    ]
    if trigger_candidates:
        return _project_activation_payload(
            row=trigger_candidates[0],
            activation_kind="triggered",
            tasks_context_name=tasks_context_name,
            destination=destination,
        )

    return None


def _project_activation_payload(
    row: _TaskRow,
    *,
    activation_kind: str,
    tasks_context_name: str,
    destination: str | None,
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
    payload = {
        "assistant_id": assistant_id,
        "destination": destination,
        "activation_key": _build_activation_key(
            assistant_id=assistant_id,
            task_id=task_id,
            destination=destination,
        ),
        "task_id": task_id,
        "source_task_log_id": row.log_event_id,
        "instance_id": _coerce_int(row.data.get("instance_id")),
        "activation_kind": activation_kind,
        "execution_mode": execution_mode,
        "browser_target": _coerce_optional_str(row.data.get("browser_target")),
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
        "max_runtime_seconds": _coerce_int(row.data.get("max_runtime_seconds")),
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


def _project_provider_event_activation_payload(
    row: _TaskRow,
    *,
    tasks_context_name: str,
    destination: str | None,
) -> dict[str, Any]:
    """Flatten one provider-event task row into an activation payload."""

    task_id = _coerce_int(row.data.get("task_id"))
    if task_id is None:
        raise ValueError("Activations require task rows with an integer task_id.")

    trigger = parse_task_trigger(row.data.get("trigger"))
    if trigger is None or trigger.kind != "provider_event":
        raise ValueError("Provider-event activations require a provider_event trigger.")

    binding_id = _coerce_optional_str(
        row.data.get("provider_event_binding_id"),
    )
    if not binding_id:
        raise ValueError(
            "Provider-event activations require provider_event_binding_id.",
        )

    assistant_id = _resolve_assistant_id(
        task_row=row,
        tasks_context_name=tasks_context_name,
    )
    execution_mode = "offline" if _coerce_bool(row.data.get("offline")) else "live"
    entrypoint = _coerce_int(row.data.get("entrypoint"))
    normalized_filters = normalize_provider_event_filters(
        [item.model_dump() for item in trigger.filters],
    )
    activation_revision = compute_provider_event_activation_revision(
        trigger=trigger,
        binding_id=binding_id,
        execution_mode=execution_mode,
        entrypoint=entrypoint,
    )
    payload = {
        "assistant_id": assistant_id,
        "destination": destination,
        "activation_key": _build_activation_key(
            assistant_id=assistant_id,
            task_id=task_id,
            destination=destination,
        ),
        "task_id": task_id,
        "source_task_log_id": row.log_event_id,
        "instance_id": _coerce_int(row.data.get("instance_id")),
        "activation_kind": "provider_event",
        "execution_mode": execution_mode,
        "status": row.data.get("status"),
        "task_name": _coerce_optional_str(row.data.get("name")),
        "task_description": _coerce_optional_str(row.data.get("description")),
        "entrypoint": entrypoint,
        "repeat": _coerce_optional_list(row.data.get("repeat")),
        "source_task_updated_at": _coerce_datetime_string(
            row.updated_at or row.created_at,
        ),
        "provider_event_binding_id": binding_id,
        "connection_id": trigger.connection_id,
        "backend_id": trigger.backend_id,
        "canonical_app_slug": trigger.canonical_app_slug,
        "event_slug": trigger.event_slug,
        "schema_version": trigger.schema_version,
        "provider_event_filters": normalized_filters,
        "activation_revision": activation_revision,
    }
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
        if previous_delete_body is not None and _scheduled_activation_delivery_identity(
            previous_delete_body,
        ) == _scheduled_activation_delivery_identity(current_upsert_body):
            return
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


def _scheduled_activation_delivery_identity(
    body: Mapping[str, Any] | None,
) -> tuple[str, int, str, str, str] | None:
    """Return the external delivery identity for one scheduled activation body."""

    if not isinstance(body, Mapping):
        return None
    assistant_id = _coerce_optional_str(body.get("assistant_id"))
    task_id = _coerce_int(body.get("task_id"))
    activation_revision = _coerce_optional_str(body.get("activation_revision"))
    scheduled_for = _coerce_datetime_string(body.get("scheduled_for"))
    execution_mode = _coerce_optional_str(body.get("execution_mode")) or "live"
    if (
        not assistant_id
        or task_id is None
        or not activation_revision
        or not scheduled_for
    ):
        return None
    return (assistant_id, task_id, activation_revision, scheduled_for, execution_mode)


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
        "destination": _coerce_optional_str(activation.get("destination")),
        "task_id": task_id,
        "activation_revision": activation_revision,
        "scheduled_for": scheduled_for,
        "execution_mode": _coerce_optional_str(activation.get("execution_mode"))
        or "live",
        "browser_target": _coerce_optional_str(activation.get("browser_target")),
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
    """Send one activation sync request to Communication when configured.

    Self-host deployments skip this sync: scheduled activations are projected
    into Orchestra and fired in-process by Unity's LocalActivationScheduler
    instead of Communication's Cloud Tasks queues.
    """

    if settings.is_self_host:
        logger.info(
            "Skipping task activation sync in self-host mode; "
            "Unity LocalActivationScheduler owns scheduled delivery.",
        )
        return

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
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                "Task activation materialization failed via Communication "
                f"{path}: HTTP {response.status_code} {response.text}",
            ) from exc


def _is_task_enabled(data: Mapping[str, Any]) -> bool:
    """Return True when a task row may arm scheduled/trigger activations.

    Missing ``enabled`` is treated as True so legacy rows without the column
    remain activatable.
    """

    if "enabled" not in data:
        return True
    return _coerce_bool(data.get("enabled"))


def _is_scheduled_activation_candidate(data: Mapping[str, Any]) -> bool:
    """Return True when a task row is the current armed scheduled activation."""

    if not _is_task_enabled(data):
        return False
    schedule = data.get("schedule")
    trigger = data.get("trigger")
    if trigger not in (None, {}):
        return False
    if not isinstance(schedule, dict):
        return False
    if schedule.get("start_at") is None:
        return False
    status = _coerce_optional_str(data.get("status"))
    return status in _SCHEDULED_ACTIVATION_STATUSES


def _is_provider_event_activation_candidate(data: Mapping[str, Any]) -> bool:
    """Return True when a task row arms a provider-event activation."""

    if not _is_task_enabled(data):
        return False
    schedule = data.get("schedule")
    if schedule not in (None, {}):
        return False
    trigger = parse_task_trigger(data.get("trigger"))
    if trigger is None or trigger.kind != "provider_event":
        return False
    if trigger.state != "enabled":
        return False
    status = _coerce_optional_str(data.get("status"))
    return status == _TRIGGERABLE_STATUS


def _is_communication_trigger_activation_candidate(data: Mapping[str, Any]) -> bool:
    """Return True when a task row arms a communication trigger activation."""

    if not _is_task_enabled(data):
        return False
    schedule = data.get("schedule")
    if schedule not in (None, {}):
        return False
    trigger = parse_task_trigger(data.get("trigger"))
    if trigger is None:
        return False
    if trigger.kind == "provider_event":
        return False
    status = _coerce_optional_str(data.get("status"))
    return status == _TRIGGERABLE_STATUS


def _is_trigger_activation_candidate(data: Mapping[str, Any]) -> bool:
    """Return True when a task row is the current armed trigger activation."""

    return _is_communication_trigger_activation_candidate(
        data,
    ) or _is_provider_event_activation_candidate(
        data,
    )


def _load_task_rows(
    session: Session,
    *,
    project_id: int,
    context_id: int,
    task_ids: Sequence[int],
) -> list[_TaskRow]:
    """Load task rows for the given logical task ids."""

    task_id_strings = [str(task_id) for task_id in task_ids]
    owner_key_filter = single_owner_key_for_context(session, context_id)
    rows = (
        session.query(
            LogEvent.id,
            LogEvent.data,
            LogEvent.updated_at,
            LogEvent.created_at,
        )
        .join(LogEventContext, log_event_context_join(owner_key=owner_key_filter))
        .filter(
            LogEvent.project_id == project_id,
            LogEventContext.project_id == project_id,
            owner_scope_clause(LogEvent, owner_key_filter),
            owner_scope_clause(LogEventContext, owner_key_filter),
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


def _projection_groups_for_task_rows(
    rows: Sequence[_TaskRow],
    *,
    task_ids: Sequence[int],
    tasks_context_name: str,
) -> list[_TaskProjectionGroup]:
    """Group task rows by the executor activation they materialize."""

    requested_task_ids = set(task_ids)
    rows_by_group: dict[tuple[str | None, int], list[_TaskRow]] = {}
    for row in rows:
        task_id = _coerce_int(row.data.get("task_id"))
        if task_id is None or task_id not in requested_task_ids:
            continue
        assistant_id = _resolve_assistant_id(
            task_row=row,
            tasks_context_name=tasks_context_name,
        )
        rows_by_group.setdefault((assistant_id, task_id), []).append(row)
    return [
        _TaskProjectionGroup(
            assistant_id=assistant_id,
            task_id=task_id,
            rows=group_rows,
        )
        for (assistant_id, task_id), group_rows in rows_by_group.items()
    ]


def _delete_activation_rows_by_task_destination(
    session: Session,
    *,
    project_id: int,
    task_id: int,
    destination: str,
) -> list[dict[str, Any]]:
    """Delete stale executor activation rows for one shared task definition."""

    rows = (
        session.query(LogEvent, LogEventContext.context_id)
        .join(LogEventContext, log_event_context_join())
        .join(Context, Context.id == LogEventContext.context_id)
        .filter(
            LogEvent.project_id == project_id,
            LogEventContext.project_id == project_id,
            Context.project_id == project_id,
            Context.name.like(f"%/{TASK_ACTIVATIONS_CONTEXT_NAME}"),
            LogEvent.data.has_key("task_id"),
            LogEvent.data.op("->>")("task_id") == str(task_id),
            LogEvent.data.has_key("destination"),
            LogEvent.data.op("->>")("destination") == destination,
        )
        .all()
    )
    deleted_payloads: list[dict[str, Any]] = []
    log_ids_by_context: dict[int, list[int]] = {}
    for log_event, context_id in rows:
        payload = dict(log_event.data or {})
        activation_key = _coerce_optional_str(
            payload.get(_TASK_ACTIVATION_UNIQUE_FIELD),
        )
        if not activation_key:
            continue
        log_ids_by_context.setdefault(int(context_id), []).append(int(log_event.id))
        deleted_payloads.append(payload)

    all_log_ids: list[int] = []
    for context_id, log_ids in log_ids_by_context.items():
        all_log_ids.extend(log_ids)
        session.execute(
            delete(LogUniqueConstraint).where(
                LogUniqueConstraint.project_id == project_id,
                LogUniqueConstraint.context_id == context_id,
                LogUniqueConstraint.log_event_id.in_(log_ids),
            ),
        )
        session.execute(
            delete(LogEventContext).where(
                LogEventContext.project_id == project_id,
                LogEventContext.context_id == context_id,
                LogEventContext.log_event_id.in_(log_ids),
            ),
        )
    if all_log_ids:
        delete_orphaned_log_events(
            session=session,
            project_id=project_id,
            skip_embedding_cleanup=True,
            log_event_ids=all_log_ids,
        )
        session.flush()
    return deleted_payloads


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
        from orchestra.db.scope import owner_from_context_name

        owner = owner_from_context_name(normalized_name)
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
                owner_scope=owner.scope.value,
                owner_id=owner.owner_id,
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
    from orchestra.db.scope import owner_key_for_context

    ok = owner_key_for_context(session, context_id)
    log_event = LogEvent(
        project_id=project_id,
        data=dict(payload),
        key_order=_extract_key_order(dict(payload)),
        created_at=now,
        updated_at=now,
        owner_key=ok,
    )
    session.add(log_event)
    session.flush()

    session.add(
        LogEventContext(
            project_id=project_id,
            log_event_id=log_event.id,
            context_id=context_id,
            owner_key=ok,
        ),
    )
    session.flush()

    inserted = session.execute(
        pg_insert(LogUniqueConstraint)
        .values(
            context_id=context_id,
            project_id=project_id,
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
            LogEventContext.project_id == project_id,
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
            LogUniqueConstraint.context_id == context_id,
            LogUniqueConstraint.project_id == project_id,
        ),
    )
    session.execute(
        delete(LogEventContext).where(
            LogEventContext.project_id == project_id,
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


def _find_machine_row_in_team_surfaces(
    session: Session,
    *,
    project_id: int,
    leaf_name: str,
    unique_field_name: str,
    unique_field_value: int | str,
) -> LogEvent | None:
    """Locate a machine row by its unique key across team task surfaces.

    Run/operation keys are idempotency keys — globally unique by
    construction (team destinations are baked into the key) — so when the
    assistant-derived context misses, the row can only live under a
    ``Teams/{id}/Tasks/{leaf}`` surface. This covers updates from runtimes
    that do not send ``source_task_log_id``.
    """

    team_context_ids = [
        context_id
        for (context_id,) in session.query(Context.id)
        .filter(
            Context.project_id == project_id,
            Context.name.like(
                f"{TEAM_CONTEXT_PREFIX}%/{TASKS_CONTEXT_NAME}/{leaf_name}",
            ),
        )
        .all()
    ]
    for context_id in team_context_ids:
        row = _get_machine_row_by_unique_field(
            session=session,
            context_id=context_id,
            unique_field_name=unique_field_name,
            unique_field_value=unique_field_value,
        )
        if row is not None:
            return row
    return None


def _get_machine_row_by_unique_field(
    session: Session,
    *,
    context_id: int,
    unique_field_name: str,
    unique_field_value: int | str,
) -> LogEvent | None:
    """Return a machine row by a top-level unique field value."""

    # Resolve the context's project so the partitioned scan prunes (the context
    # uniquely determines its project).
    project_id = (
        session.query(Context.project_id).filter(Context.id == context_id).scalar()
    )
    owner_key_filter = single_owner_key_for_context(session, context_id)
    rows = (
        session.query(LogEvent)
        .join(LogEventContext, log_event_context_join(owner_key=owner_key_filter))
        .filter(
            LogEvent.project_id == project_id,
            LogEventContext.project_id == project_id,
            owner_scope_clause(LogEvent, owner_key_filter),
            owner_scope_clause(LogEventContext, owner_key_filter),
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
