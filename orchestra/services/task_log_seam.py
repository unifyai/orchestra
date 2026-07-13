"""Classify generic log writes for provider-event task rows."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import Assistant, LogEvent
from orchestra.services.task_machine_state_service import (
    TASK_MACHINE_PROJECT_NAME,
    is_task_surface_context_name,
)
from orchestra.services.task_mutation_contract import (
    _LOG_PATCH_METADATA_KEYS,
    ProviderEventWriteRejected,
    classify_provider_event_update_fields,
    current_task_revision,
    is_provider_event_task_row,
)
from orchestra.services.task_row_field import ProviderEventUpdateKind
from orchestra.services.task_runtime_update_service import TaskRuntimeUpdateService

_PROVIDER_EVENT_AUTHORED_CREATE_USE_TYPED_TASKS_API = (
    "provider_event_authored_create_use_typed_tasks_api"
)
_PROVIDER_EVENT_AUTHORED_UPDATE_USE_TYPED_TASKS_API = (
    "provider_event_authored_update_use_typed_tasks_api"
)
_PROVIDER_EVENT_AUTHORED_DELETE_USE_TYPED_TASKS_API = (
    "provider_event_authored_delete_use_typed_tasks_api"
)


def maybe_create_provider_event_task_logs(
    session: Session,
    *,
    project_id: int,
    project_name: str | None,
    context_name: str | None,
    entries: dict[str, Any] | list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Reject provider-event task creates on the generic log path.

    Authored provider-event lifecycle writes belong on the typed Tasks API.
    Returns ``None`` when the caller should continue through the legacy path.
    """

    del session, project_id

    if project_name != TASK_MACHINE_PROJECT_NAME or not is_task_surface_context_name(
        context_name,
    ):
        return None

    entries_list = entries if isinstance(entries, list) else [entries]
    if len(entries_list) != 1:
        return None
    payload = entries_list[0]
    if not isinstance(payload, dict) or not is_provider_event_task_row(payload):
        return None
    if payload.get("task_id") is not None:
        return None

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=_PROVIDER_EVENT_AUTHORED_CREATE_USE_TYPED_TASKS_API,
    )


def maybe_apply_provider_event_log_updates(
    session: Session,
    *,
    project_id: int,
    project_name: str | None,
    context_name: str | None,
    log_ids: list[int],
    entries: dict[str, Any] | list[dict[str, Any]],
    overwrite: bool,
) -> dict[str, Any] | None:
    """Apply runtime-only provider-event updates on the generic log path.

    Authored provider-event patches must use the typed Tasks API. Returns a
    response payload when the seam handled the update, or ``None`` when the
    caller should continue through the legacy log update path.
    """

    del overwrite

    if project_name != TASK_MACHINE_PROJECT_NAME or not is_task_surface_context_name(
        context_name,
    ):
        return None

    if len(log_ids) != 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provider-event task updates must target exactly one log id.",
        )

    log_id = int(log_ids[0])
    payload = entries if isinstance(entries, dict) else entries[0]
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provider-event task updates require object entries.",
        )

    log_event = (
        session.query(LogEvent)
        .filter(
            LogEvent.project_id == project_id,
            LogEvent.id == log_id,
        )
        .one_or_none()
    )
    if log_event is None:
        return None
    existing_data = dict(log_event.data or {})
    if not is_provider_event_task_row(existing_data):
        return None

    update_fields = {
        key: value
        for key, value in payload.items()
        if key not in _LOG_PATCH_METADATA_KEYS
    }
    if not update_fields:
        return {
            "info": "No-op provider-event task update.",
            "successful_update_ids": [log_id],
        }

    try:
        classification = classify_provider_event_update_fields(
            update_fields,
            existing_data=existing_data,
        )
    except ProviderEventWriteRejected as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc.reason),
        ) from exc

    if classification is ProviderEventUpdateKind.authored:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_PROVIDER_EVENT_AUTHORED_UPDATE_USE_TYPED_TASKS_API,
        )

    assistant = _assistant_for_task_row(session, existing_data)
    if assistant is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unable to resolve assistant for provider-event task row.",
        )

    runtime_service = TaskRuntimeUpdateService(session)
    try:
        runtime_service.apply_runtime_update(
            assistant=assistant,
            log_event_id=log_id,
            updates=update_fields,
        )
    except ProviderEventWriteRejected as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc.reason),
        ) from exc
    session.commit()
    return {
        "info": "Provider-event runtime update applied.",
        "successful_update_ids": [log_id],
        "task_revision": current_task_revision(existing_data),
    }


def maybe_delete_provider_event_task_logs(
    session: Session,
    *,
    project_id: int,
    project_name: str | None,
    context_name: str | None,
    ids_and_fields: list[tuple[Any, Any]],
) -> dict[str, Any] | None:
    """Reject provider-event full-row deletes on the generic log path."""

    if project_name != TASK_MACHINE_PROJECT_NAME or not is_task_surface_context_name(
        context_name,
    ):
        return None
    if len(ids_and_fields) != 1:
        return None
    log_spec, fields = ids_and_fields[0]
    if fields is not None or not isinstance(log_spec, int):
        return None

    log_event = (
        session.query(LogEvent)
        .filter(
            LogEvent.project_id == project_id,
            LogEvent.id == int(log_spec),
        )
        .one_or_none()
    )
    if log_event is None:
        return None
    existing_data = dict(log_event.data or {})
    if not is_provider_event_task_row(existing_data):
        return None

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=_PROVIDER_EVENT_AUTHORED_DELETE_USE_TYPED_TASKS_API,
    )


def _assistant_for_task_row(
    session: Session,
    data: dict[str, Any],
) -> Assistant | None:
    assistant_id = data.get("_assistant_id") or data.get("assistant_id")
    if assistant_id is None:
        return None
    try:
        normalized_id = int(str(assistant_id))
    except (TypeError, ValueError):
        return None
    return session.query(Assistant).filter(Assistant.agent_id == normalized_id).first()
