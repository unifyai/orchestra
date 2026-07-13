"""Allowlisted runtime-only task row updates for provider-event tasks."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import Assistant
from orchestra.services.task_machine_state_service import _replace_log_payload
from orchestra.services.task_mutation_contract import (
    ProviderEventWriteRejected,
    current_task_revision,
    is_provider_event_task_row,
)
from orchestra.services.task_mutation_service import TaskMutationService
from orchestra.services.task_row_field import RuntimeTaskField, RuntimeTaskStatus


class TaskRuntimeUpdateService:
    """Apply runtime-only patches without bumping authored task revision."""

    def __init__(self, session: Session) -> None:
        self._mutation_service = TaskMutationService(session)
        self.session = session

    def apply_runtime_update(
        self,
        *,
        assistant: Assistant,
        log_event_id: int,
        updates: dict[str, Any],
    ) -> dict[str, Any]:
        """Patch allowlisted runtime fields on one provider-event task row."""

        allowed = set(updates.keys())
        if not allowed.issubset(RuntimeTaskField.values()):
            raise ProviderEventWriteRejected(reason="runtime_field_not_allowlisted")
        if RuntimeTaskField.status.value in updates and not RuntimeTaskStatus.allows(
            updates["status"],
        ):
            raise ProviderEventWriteRejected(
                reason=f"runtime_status_not_allowed:{updates['status']}",
            )

        project_id, context_id, tasks_context_name = (
            self._mutation_service._resolve_task_scope(assistant)
        )
        log_event = self._mutation_service._lock_task_row(
            project_id=project_id,
            log_event_id=log_event_id,
        )
        data = dict(log_event.data or {})
        if not is_provider_event_task_row(data):
            raise ProviderEventWriteRejected(reason="not_provider_event_task")
        merged = dict(data)
        merged.update(updates)
        revision = current_task_revision(merged)
        _replace_log_payload(log_event, merged)
        self.session.flush()
        self._mutation_service._project_task_activation(
            project_id=project_id,
            tasks_context_name=tasks_context_name,
            task_ids={int(merged["task_id"])},
        )
        merged.setdefault("task_revision", revision)
        return merged
