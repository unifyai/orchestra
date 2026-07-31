"""Field classification and shared contracts for revision-safe task writes."""

from __future__ import annotations

from typing import Any

from orchestra.provider_triggers.task_trigger import parse_task_trigger
from orchestra.services.task_row_field import (
    AuthoredTaskField,
    ProviderEventUpdateKind,
    RuntimeTaskField,
    TaskRowKey,
    TaskRowMetaField,
)

_LOG_PATCH_METADATA_KEYS = frozenset({"explicit_types", "infer_untyped_fields"})


class TaskRevisionConflict(Exception):
    """Raised when an authored task write loses a revision CAS."""

    def __init__(self, *, latest_revision: int) -> None:
        self.latest_revision = latest_revision
        super().__init__(f"task_revision_conflict:{latest_revision}")


class ProviderEventWriteRejected(Exception):
    """Raised when a provider-event task write cannot be classified safely."""

    def __init__(self, *, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def is_provider_event_task_row(data: dict[str, Any]) -> bool:
    """Return True when the task row carries a provider-event trigger."""

    trigger = parse_task_trigger(data.get("trigger"))
    return trigger is not None and trigger.kind == "provider_event"


def classify_provider_event_update_fields(
    fields: dict[str, Any],
    *,
    existing_data: dict[str, Any],
) -> ProviderEventUpdateKind:
    """Classify one provider-event update as authored or runtime.

    Raises :class:`ProviderEventWriteRejected` when the patch cannot be routed
    safely.
    """

    keys = set(fields.keys()) - _LOG_PATCH_METADATA_KEYS
    if not keys:
        raise ProviderEventWriteRejected(reason="empty_update")

    authored_keys = set(keys & AuthoredTaskField.values())
    runtime_keys = set(keys & RuntimeTaskField.values())
    unknown_keys = (
        keys
        - AuthoredTaskField.values()
        - RuntimeTaskField.values()
        - TaskRowMetaField.values()
        - {TaskRowKey.task_revision.value, TaskRowKey.provider_event_binding_id.value}
    )
    if unknown_keys:
        raise ProviderEventWriteRejected(
            reason=f"unclassified_fields:{','.join(sorted(unknown_keys))}",
        )

    if authored_keys and runtime_keys:
        raise ProviderEventWriteRejected(reason="mixed_authored_runtime_update")

    if runtime_keys:
        if runtime_keys - RuntimeTaskField.values():
            raise ProviderEventWriteRejected(reason="runtime_field_not_allowlisted")
        return ProviderEventUpdateKind.runtime

    if authored_keys:
        return ProviderEventUpdateKind.authored

    if is_provider_event_task_row(existing_data):
        raise ProviderEventWriteRejected(reason="unclassified_provider_event_update")
    raise ProviderEventWriteRejected(reason="unclassified_update")


def current_task_revision(data: dict[str, Any]) -> int:
    """Return the persisted task revision, defaulting legacy rows to 1."""

    raw = data.get(TaskRowKey.task_revision.value)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 1


def task_revision_conflict_detail(*, latest_revision: int) -> dict[str, Any]:
    """Return the stable HTTP 409 body for revision conflicts."""

    return {
        "code": "task_revision_conflict",
        "task_revision": latest_revision,
    }


def format_task_etag(task_revision: int) -> str:
    """Return the opaque ETag for one task revision."""

    return f'"{task_revision}"'


def parse_if_match(if_match: str | None) -> int:
    """Parse an HTTP If-Match header into a task revision."""

    if if_match is None or not str(if_match).strip():
        raise ValueError("missing_if_match")
    token = str(if_match).strip().strip('"')
    return int(token)
