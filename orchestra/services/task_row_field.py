"""Closed vocabularies for provider-event task row field classification."""

from __future__ import annotations

from enum import StrEnum


class AuthoredTaskField(StrEnum):
    """Task JSONB keys that require revision CAS on provider-event rows."""

    name = "name"
    description = "description"
    schedule = "schedule"
    trigger = "trigger"
    enabled = "enabled"
    offline = "offline"
    requires_filesystem = "requires_filesystem"
    requires_computer = "requires_computer"
    entrypoint = "entrypoint"
    priority = "priority"
    repeat = "repeat"
    deadline = "deadline"
    response_policy = "response_policy"
    destination = "destination"

    @classmethod
    def values(cls) -> frozenset[str]:
        return frozenset(member.value for member in cls)


class RuntimeTaskField(StrEnum):
    """Task JSONB keys that may change without bumping task_revision.

    Deliberately empty. Every field on a definition is authored: run outcome
    lives on ``Tasks/Executions``, and ``enabled`` is an operator decision that
    must take the revision CAS path. A new member here means something
    run-derived has crept back onto the row every concurrent run shares.

    Mirrored byte-for-byte in ``unify.task_scheduler.types.task_row_field`` and
    pinned by the shared contract fixture.
    """

    @classmethod
    def values(cls) -> frozenset[str]:
        return frozenset(member.value for member in cls)


class ProviderEventUpdateKind(StrEnum):
    """How one provider-event log patch should be routed."""

    authored = "authored"
    runtime = "runtime"


class TaskRowKey(StrEnum):
    """Stable JSONB keys owned by the revision-safe task contract."""

    task_revision = "task_revision"
    provider_event_binding_id = "provider_event_binding_id"


class TaskRowMetaField(StrEnum):
    """Internal task-row metadata excluded from patch classification."""

    task_id = "task_id"
    user_id = "_user_id"
    assistant_id = "_assistant_id"

    @classmethod
    def values(cls) -> frozenset[str]:
        return frozenset(member.value for member in cls)
