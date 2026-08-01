"""Internal machine-state helpers for assistant task executions.

This module keeps scheduled and triggerable task machine state inside the
existing Orchestra log/context system. The public assistant-scoped `.../Tasks`
table in the `Assistants` project remains the definition surface;
`Tasks/Executions` (wake + attempt ledger) and `Tasks/OutboundOperations`
are the internal machine contexts.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
from orchestra.provider_triggers.revision import (
    compute_provider_event_revision,
    normalize_trigger_config,
)
from orchestra.provider_triggers.task_trigger import parse_task_trigger
from orchestra.services.task_repetition import (
    deterministic_jitter_seconds,
    next_repeated_start_at,
    parse_repeat_patterns,
)
from orchestra.settings import settings

TASK_MACHINE_PROJECT_NAME = "Assistants"
TASKS_CONTEXT_NAME = "Tasks"
TASK_EXECUTIONS_CONTEXT_NAME = "Tasks/Executions"
TASK_RUNS_CONTEXT_NAME = "Tasks/Runs"
TASK_OUTBOUND_OPERATIONS_CONTEXT_NAME = "Tasks/OutboundOperations"
_ALL_CONTEXT_SEGMENT = "All"
_TASK_EXECUTIONS_CONTEXT_LEAF = "Executions"
_TASK_RUNS_CONTEXT_LEAF = "Runs"
_TASK_OUTBOUND_OPERATIONS_CONTEXT_LEAF = "OutboundOperations"
_TASK_RUN_UNIQUE_FIELD = "run_key"
_TASK_OUTBOUND_OPERATION_UNIQUE_FIELD = "operation_key"
_TASK_EXECUTION_UPSERT_PATH = "/infra/task-execution/upsert"
_TASK_EXECUTION_DELETE_PATH = "/infra/task-execution/delete"
_TASK_EXECUTION_SYNC_TIMEOUT_SECONDS = 15.0
_INTERNAL_TASK_MACHINE_CONTEXT_NAMES = frozenset(
    {
        TASK_EXECUTIONS_CONTEXT_NAME,
        TASK_OUTBOUND_OPERATIONS_CONTEXT_NAME,
        TASK_RUNS_CONTEXT_NAME,
    },
)

_OPEN_EXECUTION_STATES = {"scheduled", "triggerable"}
_DEFAULT_SCHEDULED_TASK_VISIBILITY_POLICY = "silent_by_default"
_RECURRING_WAKE_HINT = "recurring"
_ONE_OFF_WAKE_HINT = "one_off"


class _KeepCurrentHead:
    """Projection outcome meaning "the run ledger owns this head, leave it".

    Distinct from ``None``, which means "this definition arms nothing" and so
    retires any open execution. A started series keeps its current head when a
    run is in flight — its dispatcher projects the successor, and a definition
    write must not race it — and when the definition does not repeat or its
    repeat rule is exhausted, where there is no future slot to rebuild.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "KEEP_CURRENT_HEAD"


KEEP_CURRENT_HEAD = _KeepCurrentHead()

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TaskMachineContextIds:
    """Resolved context identifiers for task machine state."""

    executions_context_id: int
    outbound_operations_context_id: int

    @property
    def runs_context_id(self) -> int:
        return self.executions_context_id


@dataclass(frozen=True)
class TaskMachineContextNames:
    """Resolved assistant-scoped context names for task machine state."""

    tasks_context_name: str
    executions_context_name: str
    outbound_operations_context_name: str

    @property
    def runs_context_name(self) -> str:
        return self.executions_context_name


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
    """Rows that project into one executor-owned open execution."""

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


def build_task_executions_context_name(tasks_context_name: str) -> str:
    """Return the assistant-scoped executions context for one Tasks table."""

    return _build_task_machine_context_name(
        tasks_context_name=tasks_context_name,
        leaf_name=_TASK_EXECUTIONS_CONTEXT_LEAF,
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
        executions_context_name=build_task_executions_context_name(
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


def _build_open_execution_run_key(
    *,
    delivery: str,
    wake: str,
    assistant_id: str,
    destination: str | None,
    task_id: int,
    revision: str,
    due_at: str | None = None,
    trigger_medium: str | None = None,
) -> str:
    """Build the idempotency key for an open (scheduled/triggerable) Execution.

    Must produce byte-identical output to Unify ``build_task_run_key``: the key
    is what makes create-or-adopt converge, so any drift stops the dispatcher
    adopting the row projected here and mints a second execution for the same
    occurrence instead. Two rows per occurrence read as concurrency, and an
    overlap guard then skips every tick — a silent halt, not an error.

    Both normalizers below exist for that reason. ``team:11`` and
    ``2026-07-29T16:50:00+00:00`` were the drift that caused it.

    Unify assembles the tail from the provenance the dispatcher can see, and a
    dispatcher waking on a projected row sees only what that row carries: the
    due time on a scheduled wake, the trigger medium on a triggered one. The
    contact and message that fired a live trigger exist only once an event has
    actually arrived, and a firing carrying them is a distinct occurrence with
    its own key. A projection with neither fragment falls through to the shared
    ``once``, which is why no wake gets a tail of its own here.
    """

    revision_digest = hashlib.sha256(
        str(revision or "").encode("utf-8"),
    ).hexdigest()[:12]
    normalized_destination = _normalize_run_key_component(destination)
    destination_part = f"{normalized_destination}:" if normalized_destination else ""
    tail_parts: list[str] = []
    if wake == "scheduled":
        normalized_due = _normalize_run_datetime_fragment(due_at) if due_at else None
        if normalized_due:
            tail_parts.append(normalized_due)
    if wake == "triggered":
        normalized_medium = _normalize_run_key_component(trigger_medium)
        if normalized_medium:
            tail_parts.append(normalized_medium[:24])
    tail = "-".join(tail_parts) or "once"
    return (
        f"{delivery}:{wake}:{assistant_id}:{destination_part}{task_id}:"
        f"{revision_digest}:{tail}"
    )


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
    """Return the canonical `.../Tasks` context for one assistant when resolvable.

    Team-owned assistants have no personal root: machine state (Executions /
    Runs / OutboundOperations) lives under ``Teams/{owner_team_id}/Tasks``,
    matching the shared authored Tasks surface.
    """

    normalized_assistant_id = _coerce_optional_str(assistant_id)
    if not normalized_assistant_id:
        return None

    assistant = _get_assistant_for_task_machine_lookup(
        session=session,
        assistant_id=normalized_assistant_id,
    )
    if assistant is not None and assistant.owner_team_id is not None:
        return f"Teams/{int(assistant.owner_team_id)}/{TASKS_CONTEXT_NAME}"
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


_RUN_FIELD_DEFINITIONS: dict[str, dict[str, Any]] = {
    "assistant_id": {
        "field_type": "str",
        "mutable": False,
        "description": "Assistant identifier that owns this execution.",
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
        "description": "Idempotency key for one wake/attempt (Execution unique key).",
    },
    "wake": {
        "field_type": "str",
        "mutable": True,
        "description": "Why this execution exists: scheduled, triggered, explicit, provider_event.",
    },
    "delivery": {
        "field_type": "str",
        "mutable": True,
        "description": "Delivery lane: live or offline.",
    },
    "trigger_medium": {
        "field_type": "str",
        "mutable": True,
        "description": "Inbound medium for triggerable executions.",
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
        "description": "Whether a trigger interrupts the assistant.",
    },
    "trigger_recurring": {
        "field_type": "bool",
        "mutable": True,
        "description": "Whether the trigger re-arms after completion.",
    },
    "requires_filesystem": {
        "field_type": "bool",
        "mutable": True,
        "description": "Whether the task needs a mounted assistant filesystem.",
    },
    "requires_computer": {
        "field_type": "bool",
        "mutable": True,
        "description": "Whether the task needs the assistant desktop computer.",
    },
    "entrypoint": {
        "field_type": "int",
        "mutable": True,
        "description": "Offline function_id when delivery=offline.",
    },
    "max_runtime_seconds": {
        "field_type": "int",
        "mutable": True,
        "description": "Optional per-task execution bound.",
    },
    "recurring": {
        "field_type": "bool",
        "mutable": True,
        "description": "Whether the definition repeats; the patterns live on it.",
    },
    "source_task_updated_at": {
        "field_type": "datetime",
        "mutable": True,
        "description": "Source task row update timestamp at projection time.",
    },
    "last_materialized_at": {
        "field_type": "datetime",
        "mutable": True,
        "description": "When Communication last upserted a Cloud Task for this row.",
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
        "description": "Task row that originated this execution.",
    },
    "state": {
        "field_type": "str",
        "mutable": True,
        "description": "Current machine state for the execution lifecycle.",
    },
    "revision": {
        "field_type": "str",
        "mutable": True,
        "description": "Stable hash of the machine-facing execution contract.",
    },
    "dispatch_offset_seconds": {
        "field_type": "float",
        "mutable": True,
        "description": (
            "Seconds to add to scheduled_for when dispatching. Jitter spreads "
            "dispatch without changing the occurrence identity: scheduled_for "
            "stays canonical so it can key run_key and anchor the next slot."
        ),
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
    "provider_event_trigger_config": {
        "field_type": "dict",
        "mutable": True,
        "description": "Passthrough trigger config for the accepted provider event.",
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
    """Ensure Executions + OutboundOperations contexts exist (no Activations)."""

    context_names = _resolve_task_machine_context_names(tasks_context_name)

    executions_context_id = _upsert_context(
        session=session,
        project_id=project_id,
        name=context_names.executions_context_name,
        description="Internal wake/attempt ledger for assistant tasks (Executions).",
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
        context_id=executions_context_id,
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
        executions_context_id=executions_context_id,
        outbound_operations_context_id=outbound_operations_context_id,
    )


def sync_task_executions_for_task_ids(
    session: Session,
    project_id: int,
    task_ids: Iterable[int],
    *,
    tasks_context_name: str = TASKS_CONTEXT_NAME,
) -> dict[str, int]:
    """Project task definitions into open `Tasks/Executions` rows (wake ledger)."""

    unique_task_ids = sorted({int(task_id) for task_id in task_ids})
    if not unique_task_ids or not is_task_surface_context_name(tasks_context_name):
        return {"upserted": 0, "deleted": 0, "unchanged": 0}
    normalized_tasks_context_name = (tasks_context_name or "").strip("/")
    source_destination = _destination_from_context_name(normalized_tasks_context_name)
    source_team_id = _team_id_from_context_name(normalized_tasks_context_name)

    tasks_context_id = _get_context_id(
        session=session,
        project_id=project_id,
        name=normalized_tasks_context_name,
    )
    if tasks_context_id is None:
        return {"upserted": 0, "deleted": 0, "unchanged": 0}
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
    unchanged = 0
    materialization_pairs: list[tuple[dict[str, Any] | None, dict[str, Any] | None]] = (
        []
    )
    handled_task_ids = {group.task_id for group in projection_groups}
    for task_id in unique_task_ids:
        if task_id in handled_task_ids:
            continue
        if source_destination is not None:
            deleted_rows = _delete_open_executions_by_task_destination(
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
                deleted_rows = _delete_open_executions_by_task_destination(
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
        if source_destination is not None and not _assistant_is_team_member(
            session=session,
            assistant_id=group.assistant_id,
            team_id=source_team_id,
        ):
            execution_payload = None
        else:
            execution_payload = _build_execution_payload(
                rows=group.rows,
                tasks_context_name=normalized_tasks_context_name,
                destination=source_destination,
                session=session,
                project_id=project_id,
            )
        if execution_payload is KEEP_CURRENT_HEAD:
            continue
        if execution_payload is None:
            # Drop any prior open execution for this task/destination.
            deleted_rows = _delete_open_executions_for_task(
                session=session,
                project_id=project_id,
                context_id=context_ids.executions_context_id,
                task_id=group.task_id,
                destination=source_destination,
            )
            deleted += len(deleted_rows)
            materialization_pairs.extend((row, None) for row in deleted_rows)
            continue

        run_key = str(execution_payload["run_key"])
        existing_execution = _get_machine_row_by_unique_field(
            session=session,
            context_id=context_ids.executions_context_id,
            unique_field_name=_TASK_RUN_UNIQUE_FIELD,
            unique_field_value=run_key,
        )
        previous_execution: dict[str, Any] | None = None
        if existing_execution is not None:
            previous_execution = dict(existing_execution.data or {})
            if _projection_is_noop(previous_execution, execution_payload):
                unchanged += 1
                continue
        else:
            # Schedule/revision edits mint a new run_key. Carry the prior open
            # Execution into the upsert so Communication gets one replace
            # (previous_revision + new head), not a delete followed by create.
            stale_deleted = _delete_open_executions_for_task(
                session=session,
                project_id=project_id,
                context_id=context_ids.executions_context_id,
                task_id=group.task_id,
                destination=source_destination,
            )
            deleted += len(stale_deleted)
            if stale_deleted:
                previous_execution = stale_deleted[0]
                materialization_pairs.extend((row, None) for row in stale_deleted[1:])
        _upsert_machine_row(
            session=session,
            project_id=project_id,
            context_id=context_ids.executions_context_id,
            unique_field_name=_TASK_RUN_UNIQUE_FIELD,
            unique_field_value=run_key,
            payload=execution_payload,
        )
        materialization_pairs.append((previous_execution, execution_payload))
        upserted += 1

    session.flush()
    for previous_execution, current_execution in materialization_pairs:
        _reconcile_scheduled_execution_materialization(
            previous_execution=previous_execution,
            current_execution=current_execution,
        )
    return {"upserted": upserted, "deleted": deleted, "unchanged": unchanged}


def lookup_task_machine_executions_context_id(
    session: Session,
    project_id: int,
    *,
    tasks_context_name: str,
) -> int | None:
    """Return the executions context id when it already exists (read-only)."""

    context_names = _resolve_task_machine_context_names(tasks_context_name)
    return _get_context_id(
        session=session,
        project_id=project_id,
        name=context_names.executions_context_name,
    )


def _delete_open_executions_for_task(
    session: Session,
    *,
    project_id: int,
    context_id: int,
    task_id: int,
    destination: str | None,
) -> list[dict[str, Any]]:
    """Delete open Executions for one task (scheduled/triggerable arms)."""

    query = (
        session.query(LogEvent)
        .join(LogEventContext, log_event_context_join())
        .filter(
            LogEvent.project_id == project_id,
            LogEventContext.project_id == project_id,
            LogEventContext.context_id == context_id,
            LogEvent.data.has_key("task_id"),
            LogEvent.data.op("->>")("task_id") == str(task_id),
        )
    )
    if destination:
        query = query.filter(
            LogEvent.data.has_key("destination"),
            LogEvent.data.op("->>")("destination") == destination,
        )
    deleted_payloads: list[dict[str, Any]] = []
    log_ids: list[int] = []
    for log_event in query.all():
        payload = dict(log_event.data or {})
        state = str(payload.get("state") or "").lower()
        if state not in {"scheduled", "triggerable", ""}:
            continue
        log_ids.append(int(log_event.id))
        deleted_payloads.append(payload)
    if not log_ids:
        return []
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
    delete_orphaned_log_events(
        session=session,
        project_id=project_id,
        skip_embedding_cleanup=True,
        log_event_ids=log_ids,
    )
    session.flush()
    return deleted_payloads


def get_open_task_execution(
    session: Session,
    project_id: int,
    *,
    assistant_id: str | None,
    task_id: int,
    destination: str | None = None,
) -> LogEvent | None:
    """Return the open Execution row for one assistant/task (wake ledger).

    Lookup is read-only: it never creates machine contexts or upserts field
    types. Schema materialization belongs on write/projection paths via
    ``ensure_task_machine_contexts``.
    """

    tasks_context_name = resolve_tasks_context_name(
        session=session,
        project_id=project_id,
        assistant_id=assistant_id,
    )
    executions_context_id = lookup_task_machine_executions_context_id(
        session=session,
        project_id=project_id,
        tasks_context_name=tasks_context_name,
    )
    if executions_context_id is None:
        return None
    # A team-owned task writes `destination` on every execution it projects, so
    # omitting it here does not mean "any destination" -- the branch below reads
    # it as "rows carrying none", which no team task ever has. Callers that know
    # the destination pass it; the rest would silently look up nothing and
    # conclude the execution is missing, which is indistinguishable from a task
    # that never materialized. The surface path already encodes the owner, and
    # projection derives the destination from it the same way.
    if destination is None:
        destination = _destination_from_context_name(tasks_context_name)
    query = (
        session.query(LogEvent)
        .join(LogEventContext, log_event_context_join())
        .filter(
            LogEvent.project_id == project_id,
            LogEventContext.project_id == project_id,
            LogEventContext.context_id == executions_context_id,
            LogEvent.data.has_key("task_id"),
            LogEvent.data.op("->>")("task_id") == str(task_id),
        )
    )
    if assistant_id:
        query = query.filter(
            LogEvent.data.has_key("assistant_id"),
            LogEvent.data.op("->>")("assistant_id") == str(assistant_id),
        )
    if destination:
        query = query.filter(
            LogEvent.data.has_key("destination"),
            LogEvent.data.op("->>")("destination") == destination,
        )
    else:
        query = query.filter(
            ~LogEvent.data.has_key("destination")
            | LogEvent.data.op("->>")("destination").is_(None)
            | (LogEvent.data.op("->>")("destination") == ""),
        )
    candidates = query.order_by(LogEvent.id.desc()).all()
    for row in candidates:
        payload = row.data if isinstance(row.data, dict) else {}
        state = str(payload.get("state") or "").lower()
        if state in _OPEN_EXECUTION_STATES or not state:
            return row
    return None


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
        context_id=context_ids.executions_context_id,
        unique_field_name=_TASK_RUN_UNIQUE_FIELD,
        unique_field_value=run_key,
    )
    if existing is not None:
        return existing, False
    migrated = _migrate_legacy_machine_row_if_present(
        session=session,
        project_id=project_id,
        legacy_context_name=TASK_RUNS_CONTEXT_NAME,
        nested_context_id=context_ids.executions_context_id,
        unique_field_name=_TASK_RUN_UNIQUE_FIELD,
        unique_field_value=run_key,
    )
    if migrated is not None:
        return migrated, False

    materialized_payload = _normalize_execution_payload(dict(payload))
    created = _upsert_machine_row(
        session=session,
        project_id=project_id,
        context_id=context_ids.executions_context_id,
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
    # A newly created open occurrence must reach Communication's delayed queue
    # or nothing ever fires it: the definition-write sync cannot see it,
    # because projecting an occurrence deliberately writes no definition. Only
    # a pending row with its due time still ahead materializes — a run created
    # already running is being started by its own dispatcher right now, and a
    # row created at/after its due time is a dispatch-time create whose
    # delayed task would fire again immediately.
    if (
        created.created
        and str(created_row.data.get("state") or "") == "scheduled"
        and _occurrence_is_in_the_future(created_row.data)
    ):
        _reconcile_scheduled_execution_materialization(
            previous_execution=None,
            current_execution=dict(created_row.data or {}),
        )
    return created_row, created.created


def _occurrence_is_in_the_future(data: Mapping[str, Any]) -> bool:
    """Whether a run row's dispatch moment (due time plus jitter) is ahead."""

    due = _parse_datetime(_coerce_datetime_string(data.get("scheduled_for")))
    if due is None:
        return False
    offset = float(data.get("dispatch_offset_seconds") or 0.0)
    return due + timedelta(seconds=offset) > datetime.now(timezone.utc)


def release_stuck_task_executions(
    session: Session,
    *,
    project_id: int,
    source_task_log_id: int,
    info: str | None = None,
    run_key: str | None = None,
) -> dict[str, Any]:
    """Terminalize executions still ``running`` after their worker vanished.

    Break-glass for a crashed or killed worker whose execution never reached a
    terminal state. It operates on ``Tasks/Executions`` because that is where
    run state lives: definitions carry authored intent only, so there is no
    longer an ``active`` definition to release and nothing here can disarm a
    schedule.

    Pass ``run_key`` to release one run. Without it this releases every running
    execution under the definition, which is only safe when no sibling is meant
    to survive: recurrence projects the next occurrence at dispatch, so an
    unscoped release from a finishing worker terminalizes the successor that
    just started and the series stops advancing.

    The previous implementation wrote ``failed`` onto the definition with no
    check for whether it repeats, which permanently disarmed recurring tasks —
    the documented break-glass was itself an outage cause.
    """

    query = session.query(LogEvent).filter(
        LogEvent.project_id == project_id,
        LogEvent.data["source_task_log_id"].astext == str(int(source_task_log_id)),
        LogEvent.data["state"].astext == "running",
    )
    if run_key:
        query = query.filter(LogEvent.data["run_key"].astext == str(run_key))
    executions = query.all()
    if not executions:
        return {
            "updated": False,
            "source_task_log_id": int(source_task_log_id),
            "released_run_keys": [],
            "reason": "no_running_executions",
        }

    released: list[str] = []
    released_payloads: list[dict[str, Any]] = []
    release_info = (info or "").strip()
    for execution in executions:
        payload = dict(execution.data or {})
        payload["state"] = "failed"
        payload["completed_at"] = datetime.now(timezone.utc).isoformat()
        if release_info:
            payload["error"] = release_info
        _replace_log_payload(execution, payload)
        released_payloads.append(payload)
        run_key = _coerce_optional_str(payload.get("run_key"))
        if run_key:
            released.append(run_key)
    session.flush()
    reprojected = _reproject_head_after_release(
        session,
        project_id=project_id,
        source_task_log_id=int(source_task_log_id),
        payloads=released_payloads,
    )
    return {
        "updated": True,
        "source_task_log_id": int(source_task_log_id),
        "released_run_keys": released,
        "reprojected": reprojected,
        "reason": "released",
    }


def _reproject_head_after_release(
    session: Session,
    *,
    project_id: int,
    source_task_log_id: int,
    payloads: list[dict[str, Any]],
) -> bool:
    """Give the definition an open occurrence again after a run is released.

    Recurrence is computed in Unify at dispatch, so a worker that died before it
    got that far — an image pull failure, an OOM at startup, a SIGKILL — leaves
    the series with no open occurrence and nothing to fire one. The definition
    stays armed and simply never runs again, which is how a ten-minute tick sat
    dead until an operator noticed and nudged it by hand.

    Releasing a run is the moment we know it is over, so the head is
    re-projected here. A crash then costs one occurrence instead of the series.
    Idempotent: the projection adopts an existing open occurrence rather than
    duplicating it, and a definition that is disabled, one-shot or exhausted
    yields nothing.
    """

    task_ids = sorted(
        {
            task_id
            for task_id in (_coerce_int(p.get("task_id")) for p in payloads)
            if task_id is not None
        },
    )
    if not task_ids:
        return False
    assistant_id = next(
        (
            _coerce_optional_str(p.get("assistant_id"))
            for p in payloads
            if p.get("assistant_id")
        ),
        None,
    )
    try:
        tasks_context_name = resolve_tasks_context_name(
            session=session,
            project_id=project_id,
            assistant_id=assistant_id,
            source_task_log_id=source_task_log_id,
        )
        result = sync_task_executions_for_task_ids(
            session=session,
            project_id=project_id,
            task_ids=task_ids,
            tasks_context_name=tasks_context_name,
        )
    except Exception:
        # Releasing the run is the caller's contract and must still succeed; a
        # series left without a head is recoverable, a lost release is not.
        logger.exception(
            "Failed to re-project the head after releasing task_ids=%s; the "
            "series will not advance until a definition write re-projects it.",
            task_ids,
        )
        return False
    return bool(result.get("upserted"))


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
    under its task's own surface (a team task's executions live in
    ``Teams/{id}/Tasks/Executions``), so ``source_task_log_id`` takes precedence
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
        context_id=context_ids.executions_context_id,
        unique_field_name=_TASK_RUN_UNIQUE_FIELD,
        unique_field_value=run_key,
    )
    if existing is None:
        existing = _migrate_legacy_machine_row_if_present(
            session=session,
            project_id=project_id,
            legacy_context_name=TASK_RUNS_CONTEXT_NAME,
            nested_context_id=context_ids.executions_context_id,
            unique_field_name=_TASK_RUN_UNIQUE_FIELD,
            unique_field_value=run_key,
        )
    if existing is None:
        existing = _find_machine_row_in_team_surfaces(
            session=session,
            project_id=project_id,
            leaf_name=_TASK_EXECUTIONS_CONTEXT_LEAF,
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


def get_task_execution(
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
        context_id=context_ids.executions_context_id,
        unique_field_name=_TASK_RUN_UNIQUE_FIELD,
        unique_field_value=run_key,
    )
    if existing is not None:
        return existing
    return _migrate_legacy_machine_row_if_present(
        session=session,
        project_id=project_id,
        legacy_context_name=TASK_RUNS_CONTEXT_NAME,
        nested_context_id=context_ids.executions_context_id,
        unique_field_name=_TASK_RUN_UNIQUE_FIELD,
        unique_field_value=run_key,
    )


def get_task_execution_by_run_id(
    session: Session,
    project_id: int,
    *,
    run_id: int,
) -> LogEvent | None:
    """Return one task run row by its stable ``run_id`` (the run's log_event id).

    Executions live under an assistant-scoped ``.../Tasks/Executions`` context
    (legacy ``.../Tasks/Runs`` rows are still readable) and their ``run_id``
    equals the backing ``LogEvent.id``. Callers pass the run id received
    out-of-band (for example on a provider-event dispatch) and get the row back
    only when it is genuinely a task execution row in the given project.
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
            Context.name.like(f"%{TASK_EXECUTIONS_CONTEXT_NAME}")
            | Context.name.like(f"%{TASK_RUNS_CONTEXT_NAME}"),
        )
        .first()
    )


def get_latest_task_execution_for_task(
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
        context_ids.executions_context_id,
    )
    filters = [
        LogEvent.project_id == project_id,
        LogEventContext.project_id == project_id,
        owner_scope_clause(LogEvent, owner_key_filter),
        owner_scope_clause(LogEventContext, owner_key_filter),
        LogEventContext.context_id == context_ids.executions_context_id,
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


def _build_execution_payload(
    rows: Sequence[_TaskRow],
    *,
    tasks_context_name: str,
    destination: str | None,
    session: Session,
    project_id: int,
) -> dict[str, Any] | _KeepCurrentHead | None:
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
        row for row in ordered_rows if _is_scheduled_execution_candidate(row.data)
    ]
    if scheduled_candidates:
        return _project_execution_payload(
            row=scheduled_candidates[0],
            wake="scheduled",
            tasks_context_name=tasks_context_name,
            destination=destination,
            session=session,
            project_id=project_id,
        )

    provider_candidates = [
        row for row in ordered_rows if _is_provider_event_execution_candidate(row.data)
    ]
    if provider_candidates:
        return _project_provider_event_execution_payload(
            row=provider_candidates[0],
            tasks_context_name=tasks_context_name,
            destination=destination,
        )

    trigger_candidates = [
        row
        for row in ordered_rows
        if _is_communication_trigger_execution_candidate(row.data)
    ]
    if trigger_candidates:
        return _project_execution_payload(
            row=trigger_candidates[0],
            wake="triggered",
            tasks_context_name=tasks_context_name,
            destination=destination,
            session=session,
            project_id=project_id,
        )

    return None


def _project_execution_payload(
    row: _TaskRow,
    *,
    wake: str,
    tasks_context_name: str,
    destination: str | None,
    session: Session,
    project_id: int,
) -> dict[str, Any] | _KeepCurrentHead:
    """Flatten the chosen source task row into an open execution payload."""

    task_id = _coerce_int(row.data.get("task_id"))
    if task_id is None:
        raise ValueError("Executions require task rows with an integer task_id.")

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
    delivery = "offline" if _coerce_bool(row.data.get("offline")) else "live"
    entrypoint = _coerce_int(row.data.get("entrypoint"))
    requires_filesystem = _requires_filesystem_from_row(row.data)
    requires_computer = _requires_computer_from_row(row.data)
    # `schedule.start_at` is the series anchor, not the next due time. Unify
    # projects each occurrence as its own execution keyed on `scheduled_for`,
    # so an existing open execution is authoritative and the anchor is only a
    # fallback for a series whose first occurrence has not been projected yet.
    # Occurrences only ever advance from the anchor, so an open execution
    # falling before it belongs to a schedule the author has since replaced;
    # ignoring it lets the edit mint a new run_key and retire the old head.
    anchor = _coerce_datetime_string(schedule.get("start_at"))
    dispatch_offset_seconds = 0.0
    scheduled_for = _open_execution_scheduled_for(
        session,
        project_id=project_id,
        source_task_log_id=row.log_event_id,
        not_before=anchor,
    )
    if scheduled_for is None:
        # Falling back to the anchor is right for a series the ledger has not
        # reached yet — a new definition, or one whose author just moved the
        # anchor ahead of every occurrence so far. It is wrong for a series that
        # has already run past it: rebuilding a head on a consumed occurrence
        # deleted the correct future head the runtime had projected, which is
        # why a live schedule once showed its next run stuck on its start time.
        # A started series with no open occurrence is instead advanced from its
        # repeat rule — unless a run is in flight, in which case its dispatcher
        # owns the projection and this write must not race it.
        latest = _latest_ledger_occurrence(
            session,
            project_id=project_id,
            source_task_log_id=row.log_event_id,
        )
        if latest is not None and latest >= (_parse_datetime(anchor) or latest):
            if _has_running_execution(
                session,
                project_id=project_id,
                source_task_log_id=row.log_event_id,
            ):
                return KEEP_CURRENT_HEAD
            minted = _next_repeat_occurrence_after(
                data=row.data,
                task_id=task_id,
                previous_start=latest,
            )
            if minted is None:
                return KEEP_CURRENT_HEAD
            scheduled_for, dispatch_offset_seconds = minted
        else:
            scheduled_for = anchor
    payload = {
        "assistant_id": assistant_id,
        "destination": destination,
        "task_id": task_id,
        "source_task_log_id": row.log_event_id,
        "wake": wake,
        "delivery": delivery,
        "state": ("scheduled" if wake == "scheduled" else "triggerable"),
        "requires_filesystem": requires_filesystem,
        "requires_computer": requires_computer,
        "task_name": _coerce_optional_str(row.data.get("name")),
        "scheduled_for": scheduled_for,
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
        "recurring": bool(row.data.get("repeat")),
        "source_task_updated_at": _coerce_datetime_string(
            row.updated_at or row.created_at,
        ),
    }
    payload["revision"] = _authored_revision(payload)
    # Post-hash: jitter spreads dispatch without changing the occurrence
    # identity, so it must not perturb the revision (or the run_key digested
    # from it) that concurrent writers converge on.
    payload["dispatch_offset_seconds"] = dispatch_offset_seconds
    payload["run_key"] = _build_open_execution_run_key(
        delivery=delivery,
        wake=wake,
        assistant_id=assistant_id or "",
        destination=destination,
        task_id=task_id,
        revision=payload["revision"],
        due_at=scheduled_for,
        trigger_medium=payload["trigger_medium"],
    )
    payload["last_materialized_at"] = _coerce_datetime_string(
        datetime.now(timezone.utc),
    )
    return payload


def _project_provider_event_execution_payload(
    row: _TaskRow,
    *,
    tasks_context_name: str,
    destination: str | None,
) -> dict[str, Any]:
    """Flatten one provider-event task row into an open execution payload."""

    task_id = _coerce_int(row.data.get("task_id"))
    if task_id is None:
        raise ValueError("Executions require task rows with an integer task_id.")

    trigger = parse_task_trigger(row.data.get("trigger"))
    if trigger is None or trigger.kind != "provider_event":
        raise ValueError("Provider-event executions require a provider_event trigger.")

    binding_id = _coerce_optional_str(
        row.data.get("provider_event_binding_id"),
    )
    if not binding_id:
        raise ValueError(
            "Provider-event executions require provider_event_binding_id.",
        )

    assistant_id = _resolve_assistant_id(
        task_row=row,
        tasks_context_name=tasks_context_name,
    )
    delivery = "offline" if _coerce_bool(row.data.get("offline")) else "live"
    entrypoint = _coerce_int(row.data.get("entrypoint"))
    requires_filesystem = _requires_filesystem_from_row(row.data)
    requires_computer = _requires_computer_from_row(row.data)
    normalized_trigger_config = normalize_trigger_config(trigger.trigger_config)
    revision = compute_provider_event_revision(
        trigger=trigger,
        binding_id=binding_id,
        execution_mode=delivery,
        entrypoint=entrypoint,
        requires_filesystem=requires_filesystem,
        requires_computer=requires_computer,
    )
    payload = {
        "assistant_id": assistant_id,
        "destination": destination,
        "task_id": task_id,
        "source_task_log_id": row.log_event_id,
        "wake": "provider_event",
        "delivery": delivery,
        "state": "triggerable",
        "requires_filesystem": requires_filesystem,
        "requires_computer": requires_computer,
        "task_name": _coerce_optional_str(row.data.get("name")),
        "entrypoint": entrypoint,
        "recurring": bool(row.data.get("repeat")),
        "source_task_updated_at": _coerce_datetime_string(
            row.updated_at or row.created_at,
        ),
        "provider_event_binding_id": binding_id,
        "connection_id": trigger.connection_id,
        "backend_id": trigger.backend_id,
        "canonical_app_slug": trigger.canonical_app_slug,
        "provider_trigger_slug": trigger.provider_trigger_slug,
        "provider_event_trigger_config": normalized_trigger_config,
        "revision": revision,
    }
    payload["run_key"] = _build_open_execution_run_key(
        delivery=delivery,
        wake="provider_event",
        assistant_id=assistant_id or "",
        destination=destination,
        task_id=task_id,
        revision=revision,
    )
    payload["last_materialized_at"] = _coerce_datetime_string(
        datetime.now(timezone.utc),
    )
    return payload


def _normalize_execution_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize execution field contract; strip obsolete identity keys."""

    normalized = dict(payload)
    for obsolete_key in (
        "source_type",
        "execution_mode",
        "activation_revision",
        "activation_key",
        "activation_kind",
        "next_due_at",
        "instance_id",
        "task_description",
        "repeat",
        "previous_error",
    ):
        normalized.pop(obsolete_key, None)
    state = str(normalized.get("state") or "").lower()
    if state in {"", "pending"}:
        wake = str(normalized.get("wake") or "").lower()
        normalized["state"] = "scheduled" if wake == "scheduled" else "running"
    return normalized


def _reconcile_scheduled_execution_materialization(
    *,
    previous_execution: Mapping[str, Any] | None,
    current_execution: Mapping[str, Any] | None,
) -> None:
    """Mirror scheduled execution changes into Communication's delayed queue."""

    current_upsert_body = _scheduled_execution_upsert_body(current_execution)
    previous_delete_body = _scheduled_execution_delete_body(previous_execution)
    if current_upsert_body is not None:
        if previous_delete_body is not None and _scheduled_execution_delivery_identity(
            previous_delete_body,
        ) == _scheduled_execution_delivery_identity(current_upsert_body):
            return
        if previous_delete_body is not None:
            current_upsert_body["previous_revision"] = previous_delete_body["revision"]
            current_upsert_body["previous_scheduled_for"] = previous_delete_body[
                "scheduled_for"
            ]
            current_upsert_body["previous_delivery"] = previous_delete_body["delivery"]
        _post_task_execution_request(
            path=_TASK_EXECUTION_UPSERT_PATH,
            body=current_upsert_body,
        )
        return
    if previous_delete_body is not None:
        _post_task_execution_request(
            path=_TASK_EXECUTION_DELETE_PATH,
            body=previous_delete_body,
        )


def _scheduled_execution_delivery_identity(
    body: Mapping[str, Any] | None,
) -> tuple[str, int, str, str, str] | None:
    """Return the external delivery identity for one scheduled execution body."""

    if not isinstance(body, Mapping):
        return None
    assistant_id = _coerce_optional_str(body.get("assistant_id"))
    task_id = _coerce_int(body.get("task_id"))
    revision = _coerce_optional_str(body.get("revision"))
    scheduled_for = _coerce_datetime_string(body.get("scheduled_for"))
    delivery = _coerce_optional_str(body.get("delivery")) or "live"
    if not assistant_id or task_id is None or not revision or not scheduled_for:
        return None
    return (assistant_id, task_id, revision, scheduled_for, delivery)


def _scheduled_execution_snapshot(
    execution: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Return the shared scheduled-execution fields Communication expects."""

    if not _is_scheduled_execution_payload(execution):
        return None
    assistant_id = _coerce_optional_str(execution.get("assistant_id"))
    task_id = _coerce_int(execution.get("task_id"))
    # Unify-projected occurrences carry revision "" (their run_key digested
    # that value), so an empty revision is a real identity here, not a gap.
    # Communication rebuilds the run key from this field at fire time; sending
    # anything else would mint a second execution for the same occurrence.
    revision = _coerce_optional_str(execution.get("revision")) or ""
    scheduled_for = _coerce_datetime_string(execution.get("scheduled_for"))
    if not assistant_id or task_id is None or not scheduled_for:
        return None
    return {
        "assistant_id": assistant_id,
        "destination": _coerce_optional_str(execution.get("destination")),
        "task_id": task_id,
        "revision": revision,
        "scheduled_for": scheduled_for,
        "dispatch_offset_seconds": float(
            execution.get("dispatch_offset_seconds") or 0.0,
        ),
        "delivery": _coerce_optional_str(execution.get("delivery")) or "live",
        "requires_filesystem": _requires_filesystem_from_row(execution),
        "requires_computer": _requires_computer_from_row(execution),
    }


def _scheduled_execution_wake_context(
    execution: Mapping[str, Any],
) -> dict[str, str]:
    """Return the compact human-facing wake context for one scheduled execution."""

    task_id = _coerce_int(execution.get("task_id"))
    task_label = _coerce_optional_str(execution.get("task_name")) or (
        f"task {task_id}" if task_id is not None else "scheduled task"
    )
    recurrence_hint = (
        _RECURRING_WAKE_HINT if execution.get("recurring") else _ONE_OFF_WAKE_HINT
    )
    return {
        "task_label": task_label,
        "task_summary": task_label,
        "visibility_policy": _DEFAULT_SCHEDULED_TASK_VISIBILITY_POLICY,
        "recurrence_hint": recurrence_hint,
    }


def _scheduled_execution_upsert_body(
    execution: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Build the Communication upsert payload for one scheduled execution."""

    snapshot = _scheduled_execution_snapshot(execution)
    if snapshot is None:
        return None
    source_task_log_id = _coerce_int(execution.get("source_task_log_id"))
    if source_task_log_id is None:
        return None
    return {
        **snapshot,
        "source_task_log_id": source_task_log_id,
        "wake": "scheduled",
        **_scheduled_execution_wake_context(execution),
    }


def _scheduled_execution_delete_body(
    execution: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Build the Communication delete payload for one scheduled execution."""

    return _scheduled_execution_snapshot(execution)


def _is_scheduled_execution_payload(execution: Mapping[str, Any] | None) -> bool:
    """Return True when the payload represents a scheduled execution snapshot."""

    if not isinstance(execution, Mapping):
        return False
    return _coerce_optional_str(execution.get("wake")) == "scheduled"


def _post_task_execution_request(*, path: str, body: Mapping[str, Any]) -> None:
    """Send one execution sync request to Communication when configured.

    Self-host deployments skip this sync: scheduled executions are projected
    into Orchestra and fired in-process by Unity's LocalActivationScheduler
    instead of Communication's Cloud Tasks queues.
    """

    if settings.is_self_host:
        logger.info(
            "Skipping task execution sync in self-host mode; "
            "Unity LocalActivationScheduler owns scheduled delivery.",
        )
        return

    comms_url = os.environ.get("UNITY_COMMS_URL", "").rstrip("/")
    admin_key = os.environ.get("ORCHESTRA_ADMIN_KEY", "")
    if not comms_url or not admin_key:
        logger.info(
            "Skipping task execution sync because UNITY_COMMS_URL or ORCHESTRA_ADMIN_KEY is missing.",
        )
        return
    with httpx.Client() as client:
        response = client.post(
            f"{comms_url}{path}",
            headers={"Authorization": f"Bearer {admin_key}"},
            json=dict(body),
            timeout=_TASK_EXECUTION_SYNC_TIMEOUT_SECONDS,
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                "Task execution materialization failed via Communication "
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


def _next_repeat_occurrence_after(
    *,
    data: Mapping[str, Any],
    task_id: int,
    previous_start: datetime,
) -> tuple[str, float] | None:
    """Mint the next future slot for a repeating series left without a head.

    A worker that dies before dispatch never projects its successor, so the
    series sits armed with no open occurrence and nothing to fire one. The
    repeat rule on the definition names the slot the runtime would have chosen;
    advancing it past *now* (rather than past the consumed occurrence) means a
    crash costs the occurrences inside the outage window, never the series.
    Returns ``None`` for a definition that does not repeat or is exhausted.
    """

    patterns = parse_repeat_patterns(data.get("repeat"))
    if not patterns:
        return None
    next_start = next_repeated_start_at(
        previous_start=previous_start,
        patterns=patterns,
        current_occurrence_index=0,
        now=datetime.now(timezone.utc),
    )
    if next_start is None:
        return None
    offset = deterministic_jitter_seconds(
        task_id=task_id,
        slot=next_start,
        patterns=patterns,
    )
    return next_start.isoformat(), offset


def _has_running_execution(
    session: Session,
    *,
    project_id: int,
    source_task_log_id: int,
) -> bool:
    """Whether any execution of this definition is currently running."""

    return (
        session.query(LogEvent.id)
        .filter(
            LogEvent.project_id == project_id,
            LogEvent.data["source_task_log_id"].astext == str(int(source_task_log_id)),
            LogEvent.data["state"].astext == "running",
        )
        .first()
    ) is not None


def _latest_ledger_occurrence(
    session: Session,
    *,
    project_id: int,
    source_task_log_id: int,
) -> datetime | None:
    """Newest occurrence this definition has materialized, in any state."""

    rows = (
        session.query(LogEvent)
        .filter(
            LogEvent.project_id == project_id,
            LogEvent.data["source_task_log_id"].astext == str(int(source_task_log_id)),
        )
        .all()
    )
    occurrences = [
        parsed
        for parsed in (
            _parse_datetime(
                _coerce_datetime_string((row.data or {}).get("scheduled_for")),
            )
            for row in rows
        )
        if parsed is not None
    ]
    return max(occurrences) if occurrences else None


def _open_execution_scheduled_for(
    session: Session,
    *,
    project_id: int,
    source_task_log_id: int,
    not_before: str | None = None,
) -> str | None:
    """Earliest open occurrence already projected for one definition.

    ``not_before`` discards occurrences projected from a superseded schedule.
    """

    rows = (
        session.query(LogEvent)
        .filter(
            LogEvent.project_id == project_id,
            LogEvent.data["source_task_log_id"].astext == str(int(source_task_log_id)),
            LogEvent.data["state"].astext.in_(("scheduled", "triggerable")),
        )
        .all()
    )
    floor = _parse_datetime(not_before)
    due = []
    for row in rows:
        value = _coerce_datetime_string((row.data or {}).get("scheduled_for"))
        parsed = _parse_datetime(value)
        if not value or parsed is None:
            continue
        if floor is not None and parsed < floor:
            continue
        due.append((parsed, value))
    return min(due)[1] if due else None


def _parse_datetime(value: str | None) -> datetime | None:
    """Parse an ISO-8601 string into an aware UTC datetime, or None."""

    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _is_scheduled_execution_candidate(data: Mapping[str, Any]) -> bool:
    """Return True when a task row is the current armed scheduled execution."""

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
    return True


def _is_provider_event_execution_candidate(data: Mapping[str, Any]) -> bool:
    """Return True when a task row arms a provider-event execution."""

    if not _is_task_enabled(data):
        return False
    schedule = data.get("schedule")
    if schedule not in (None, {}):
        return False
    trigger = parse_task_trigger(data.get("trigger"))
    if trigger is None or trigger.kind != "provider_event":
        return False
    return trigger.state == "enabled"


def _is_communication_trigger_execution_candidate(data: Mapping[str, Any]) -> bool:
    """Return True when a task row arms a communication trigger execution."""

    if not _is_task_enabled(data):
        return False
    schedule = data.get("schedule")
    if schedule not in (None, {}):
        return False
    trigger = parse_task_trigger(data.get("trigger"))
    if trigger is None:
        return False
    return trigger.kind != "provider_event"


def _is_trigger_execution_candidate(data: Mapping[str, Any]) -> bool:
    """Return True when a task row is the current armed trigger execution."""

    return _is_communication_trigger_execution_candidate(
        data,
    ) or _is_provider_event_execution_candidate(
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
    """Group task rows by the executor open execution they materialize."""

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


def _delete_open_executions_by_task_destination(
    session: Session,
    *,
    project_id: int,
    task_id: int,
    destination: str,
) -> list[dict[str, Any]]:
    """Delete stale executor execution rows for one shared task definition."""

    rows = (
        session.query(LogEvent, LogEventContext.context_id)
        .join(LogEventContext, log_event_context_join())
        .join(Context, Context.id == LogEventContext.context_id)
        .filter(
            LogEvent.project_id == project_id,
            LogEventContext.project_id == project_id,
            Context.project_id == project_id,
            Context.name.like(f"%/{TASK_EXECUTIONS_CONTEXT_NAME}"),
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
        state = str(payload.get("state") or "").lower()
        if state not in _OPEN_EXECUTION_STATES and state:
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
                "ui_editable": definition.get("ui_editable", True),
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
            "ui_editable": stmt.excluded.ui_editable,
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


def _requires_filesystem_from_row(data: Mapping[str, Any] | None) -> bool:
    return _coerce_bool((data or {}).get("requires_filesystem"))


def _requires_computer_from_row(data: Mapping[str, Any] | None) -> bool:
    return _coerce_bool((data or {}).get("requires_computer"))


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


# The authored facts that decide what an occurrence is and does. Anything
# absent here is either derived (``state`` follows ``wake``), cosmetic
# (``task_name``), or provenance (``source_task_updated_at``).
_REVISION_AUTHORED_KEYS = (
    "assistant_id",
    "destination",
    "task_id",
    "source_task_log_id",
    "wake",
    "delivery",
    "scheduled_for",
    "entrypoint",
    "max_runtime_seconds",
    "requires_filesystem",
    "requires_computer",
    "recurring",
    "trigger_medium",
    "trigger_from_contact_ids",
    "trigger_omit_contact_ids",
    "trigger_recurring",
    "interrupt",
)


def _authored_revision(payload: Mapping[str, Any]) -> str:
    """Fingerprint the authored facts that govern one occurrence.

    Deliberately not a hash of the whole projected payload. That payload
    carries provenance and whatever columns the projection happens to
    write this month, so hashing it made schema evolution
    indistinguishable from an authored edit: every armed head in the
    fleet re-keyed whenever a projected field was added or removed. It
    also folded in ``source_task_updated_at``, so a sync re-stamp that
    changed nothing a run cares about still retired the live head.

    Only facts that change what this occurrence *is* or *does* belong
    here. A rename does not: the same run under a new label is still the
    same run.
    """

    return _stable_hash({key: payload.get(key) for key in _REVISION_AUTHORED_KEYS})


# Jitter and materialization bookkeeping are deliberately not part of an
# occurrence's identity, so they never make a re-projection meaningful.
_PROJECTION_VOLATILE_KEYS = frozenset(
    {
        "dispatch_offset_seconds",
        "last_materialized_at",
    },
)


def _projection_is_noop(
    stored: Mapping[str, Any] | None,
    payload: Mapping[str, Any],
) -> bool:
    """True when the stored head already asserts everything the projection would.

    Projection is idempotent by construction, so re-running it over an
    unchanged fleet used to rewrite every open head and count each rewrite
    as an upsert. That made the supervisor sweep a write amplifier at its
    tick rate, and — worse — made its headline number meaningless: a
    perfectly healthy fleet reported one "upsert" per definition, which is
    exactly the signal operators read as "the sweep healed something".
    """

    if stored is None:
        return False
    for key, value in payload.items():
        if key in _PROJECTION_VOLATILE_KEYS:
            continue
        if stored.get(key) != value:
            return False
    return True


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


def _normalize_run_key_component(value: Any) -> str | None:
    """Normalize one free-form run-key component into a compact identifier.

    Mirrors Unify ``_normalize_run_key_component``. See
    ``_build_open_execution_run_key`` for why the two must not drift.
    """

    text = _coerce_optional_str(value)
    if not text:
        return None
    normalized = "".join(
        char.lower() if char.isalnum() else "-" for char in text.strip()
    ).strip("-")
    while "--" in normalized:
        normalized = normalized.replace("--", "-")
    return normalized or None


def _normalize_run_datetime_fragment(value: Any) -> str | None:
    """Normalize a datetime into the canonical run-key timestamp fragment.

    Mirrors Unify ``_normalize_run_datetime_fragment``.
    """

    parsed = _parse_datetime(_coerce_datetime_string(value))
    if parsed is None:
        return None
    return parsed.strftime("%Y%m%dT%H%M%SZ")
