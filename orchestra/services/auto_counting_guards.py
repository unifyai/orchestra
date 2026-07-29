"""Guards for auto-counted unique identity fields (e.g. Tasks.task_id).

Hand-editing these fields desyncs ``context_counter`` and creates duplicate
identities. Public log updates must refuse mutations; allocation must never
reissue an id that already exists in the context.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import Context, LogEvent, LogEventContext


def protected_auto_counting_fields(context: Context | None) -> set[str]:
    """Unique keys that are also auto-counted (server-assigned identity)."""

    if context is None:
        return set()
    auto = context.auto_counting or {}
    unique_names = list(context.unique_key_names or [])
    if not unique_names and context.unique_keys:
        unique_names = list(context.unique_keys.keys())
    return {name for name in unique_names if name in auto}


def _coerce_comparable(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def reject_auto_counting_identity_mutations(
    *,
    session: Session,
    context: Context | None,
    log_ids: list[int],
    entries: Any,
) -> None:
    """Raise 400 if ``entries`` would change a protected auto-counting unique key."""

    protected = protected_auto_counting_fields(context)
    if not protected or not log_ids or entries is None:
        return

    for index, log_id in enumerate(log_ids):
        if isinstance(entries, dict):
            this_data = entries
        else:
            try:
                this_data = entries[index]
            except (IndexError, TypeError, KeyError):
                continue
        if not isinstance(this_data, dict):
            continue
        colliding = protected.intersection(this_data.keys())
        if not colliding:
            continue
        # Prune to the context's project: log_event is partitioned by
        # project_id, and an id-only lookup fans out across every tenant's
        # partition (the June production slowdown class of query).
        row = (
            session.query(LogEvent)
            .filter(
                LogEvent.project_id == int(context.project_id),
                LogEvent.id == int(log_id),
            )
            .first()
        )
        if row is None:
            continue
        current = row.data if isinstance(row.data, dict) else {}
        for field in sorted(colliding):
            if field == "explicit_types":
                continue
            new_value = this_data.get(field)
            old_value = current.get(field)
            if _coerce_comparable(new_value) == _coerce_comparable(old_value):
                continue
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Field '{field}' is an auto-counted unique identity on this "
                    "context and cannot be modified after create. Use TaskScheduler "
                    "APIs / POST /tasks/{id}/trigger / "
                    "POST /admin/task-source/release-active instead of rewriting "
                    "identity fields."
                ),
            )


def reject_atomic_auto_counting_identity_mutation(
    *,
    session: Session,
    log_id: int,
    field_name: str,
) -> None:
    """Refuse atomic updates that target a protected auto-counting unique key."""

    contexts = (
        session.query(Context)
        .join(LogEventContext, LogEventContext.context_id == Context.id)
        .filter(LogEventContext.log_event_id == int(log_id))
        .all()
    )
    for context in contexts:
        if field_name in protected_auto_counting_fields(context):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Field '{field_name}' is an auto-counted unique identity on "
                    "this context and cannot be modified after create."
                ),
            )
