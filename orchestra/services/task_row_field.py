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
    browser_target = "browser_target"
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
    """Task JSONB keys that may change without bumping task_revision."""

    status = "status"
    activated_by = "activated_by"
    instance_id = "instance_id"

    @classmethod
    def values(cls) -> frozenset[str]:
        return frozenset(member.value for member in cls)


class RuntimeTaskStatus(StrEnum):
    """Runtime status values allowed on provider-event rows without revision bump."""

    active = "active"
    completed = "completed"
    cancelled = "cancelled"
    failed = "failed"

    @classmethod
    def values(cls) -> frozenset[str]:
        return frozenset(member.value for member in cls)

    @classmethod
    def allows(cls, value: object) -> bool:
        if value is None:
            return False
        try:
            cls(str(value))
        except ValueError:
            return False
        return True


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
