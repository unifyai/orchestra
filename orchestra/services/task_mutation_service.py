"""Revision-safe authored task mutations for provider-event and typed APIs."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete
from sqlalchemy.orm import Session

from orchestra.db.dao.context_dao import ContextDAO, delete_orphaned_log_events
from orchestra.db.dao.field_type_dao import FieldTypeDAO
from orchestra.db.dao.log_event_dao import LogEventDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.project_dao import ProjectDAO
from orchestra.db.dao.provider_trigger_dao import ProviderTriggerDAO
from orchestra.db.log_queries import log_event_context_join, owner_scope_clause
from orchestra.db.models.orchestra_models import (
    Assistant,
    Context,
    LogEvent,
    LogEventContext,
    LogUniqueConstraint,
)
from orchestra.db.scope import single_owner_key_for_context
from orchestra.provider_triggers.task_trigger import (
    ProviderEventTrigger,
    parse_task_trigger,
)
from orchestra.services.staged_trigger_catalog_service import (
    validate_provider_event_trigger_for_assistant,
)
from orchestra.services.task_machine_state_service import (
    TASK_MACHINE_PROJECT_NAME,
    _coerce_bool,
    _coerce_int,
    _replace_log_payload,
    _requires_computer_from_row,
    _requires_filesystem_from_row,
    resolve_tasks_context_name,
    sync_task_executions_for_task_ids,
)
from orchestra.services.task_mutation_contract import (
    ProviderEventWriteRejected,
    TaskRevisionConflict,
    classify_provider_event_update_fields,
    current_task_revision,
    is_provider_event_task_row,
)
from orchestra.services.task_row_field import ProviderEventUpdateKind, TaskRowKey
from orchestra.web.api.log.schema import CreateLogConfig
from orchestra.web.api.log.utils.logging_utils import create_logs_internal


@dataclass(frozen=True)
class TaskRowSnapshot:
    """One authored task row bound to a log event."""

    log_event_id: int
    task_id: int
    data: dict[str, Any]
    task_revision: int


@dataclass(frozen=True)
class TaskMutationResult:
    """Outcome of one revision-safe authored task mutation."""

    log_event_id: int
    task_id: int
    task_revision: int
    data: dict[str, Any]


class TaskMutationService:
    """Apply revision-safe authored mutations to assistant Tasks rows."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self._context_dao = ContextDAO(session)
        self._organization_member_dao = OrganizationMemberDAO(session)
        self._project_dao = ProjectDAO(
            session,
            self._organization_member_dao,
            self._context_dao,
        )
        self._field_type_dao = FieldTypeDAO(session)
        self._log_event_dao = LogEventDAO(session, self._context_dao)
        self._provider_trigger_dao = ProviderTriggerDAO(session)

    def list_tasks(
        self,
        *,
        assistant: Assistant,
        limit: int = 100,
    ) -> list[TaskRowSnapshot]:
        """Return the latest row per logical task id for one assistant."""

        project_id, context_id, tasks_context_name = self._resolve_task_scope(assistant)
        rows = self._load_task_rows(
            project_id=project_id,
            context_id=context_id,
            task_id=None,
            limit=limit,
        )
        latest_by_task_id: dict[int, TaskRowSnapshot] = {}
        for row in rows:
            task_id = _coerce_int(row.data.get("task_id"))
            if task_id is None:
                continue
            existing = latest_by_task_id.get(task_id)
            if existing is None or row.log_event_id > existing.log_event_id:
                latest_by_task_id[task_id] = row
        return [latest_by_task_id[task_id] for task_id in sorted(latest_by_task_id)]

    def get_task(
        self,
        *,
        assistant: Assistant,
        task_id: int,
    ) -> TaskRowSnapshot | None:
        """Return the current authored row for one logical task id."""

        project_id, context_id, _ = self._resolve_task_scope(assistant)
        return self._get_latest_task_row(
            project_id=project_id,
            context_id=context_id,
            task_id=task_id,
        )

    def get_binding_for_task(
        self,
        *,
        assistant: Assistant,
        task_id: int,
        for_update: bool = False,
    ):
        """Return the durable binding for one provider-event task, if present."""

        project_id, context_id, _ = self._resolve_task_scope(assistant)
        row = self._get_latest_task_row(
            project_id=project_id,
            context_id=context_id,
            task_id=task_id,
        )
        if row is None:
            return None
        return self._provider_trigger_dao.get_binding_for_task(
            project_id=project_id,
            source_task_log_id=row.log_event_id,
            for_update=for_update,
        )

    def create_task(
        self,
        *,
        assistant: Assistant,
        entries: dict[str, Any],
    ) -> TaskMutationResult:
        """Create one authored task row at revision 1."""

        project_id, context_id, tasks_context_name = self._resolve_task_scope(assistant)
        payload = dict(entries)
        payload.setdefault("_user_id", str(assistant.user_id))
        payload.setdefault("_assistant_id", str(assistant.agent_id))
        payload[TaskRowKey.task_revision.value] = 1
        payload.setdefault("enabled", True)
        payload.setdefault("priority", "normal")
        if "task_id" not in payload:
            payload["task_id"] = self._allocate_task_id(
                project_id=project_id,
                context_id=context_id,
            )
        payload.setdefault("instance_id", 0)

        trigger = parse_task_trigger(payload.get("trigger"))
        binding_id: str | None = None
        if trigger is not None and trigger.kind == "provider_event":
            if isinstance(trigger, ProviderEventTrigger):
                validate_provider_event_trigger_for_assistant(
                    self.session,
                    assistant_id=int(assistant.agent_id),
                    trigger=trigger,
                )
            binding_id = f"binding-{uuid.uuid4().hex[:12]}"
            payload[TaskRowKey.provider_event_binding_id.value] = binding_id

        request = CreateLogConfig(
            project_name=TASK_MACHINE_PROJECT_NAME,
            context=tasks_context_name,
            entries=payload,
        )
        context_obj = self.session.get(Context, context_id)
        result = create_logs_internal(
            request=request,
            project_id=project_id,
            context_id=context_id,
            context_obj=context_obj,
            project_dao=self._project_dao,
            field_type_dao=self._field_type_dao,
            log_event_dao=self._log_event_dao,
            context_dao=self._context_dao,
        )
        log_event_ids = result.get("log_event_ids") or []
        if not log_event_ids:
            first_error = (result.get("failed") or [{}])[0].get(
                "error",
                "task_create_failed",
            )
            raise ValueError(first_error)

        log_event_id = int(log_event_ids[0])
        created = self._lock_task_row(
            project_id=project_id,
            log_event_id=log_event_id,
        )
        data = dict(created.data or {})
        task_id = int(data["task_id"])

        if binding_id is not None and isinstance(trigger, ProviderEventTrigger):
            execution_mode = "offline" if _coerce_bool(data.get("offline")) else "live"
            self._provider_trigger_dao.create_binding(
                binding_id=binding_id,
                project_id=project_id,
                tasks_context_id=context_id,
                source_task_log_id=log_event_id,
                task_id=task_id,
                assistant_id=int(assistant.agent_id),
                task_revision=1,
                trigger=trigger,
                execution_mode=execution_mode,
                entrypoint=_coerce_int(data.get("entrypoint")),
                task_enabled=_coerce_bool(data.get("enabled", True)),
                requires_filesystem=_requires_filesystem_from_row(data),
                requires_computer=_requires_computer_from_row(data),
            )

        self._project_task_executions(
            project_id=project_id,
            tasks_context_name=tasks_context_name,
            task_ids={task_id},
        )
        return TaskMutationResult(
            log_event_id=log_event_id,
            task_id=task_id,
            task_revision=current_task_revision(data),
            data=data,
        )

    def mutate_authored_task(
        self,
        *,
        assistant: Assistant,
        task_id: int,
        expected_task_revision: int,
        updates: dict[str, Any],
        bump_acceptance_epoch: bool = True,
    ) -> TaskMutationResult:
        """Apply one authored update under a revision CAS."""

        project_id, context_id, tasks_context_name = self._resolve_task_scope(assistant)
        row = self._get_latest_task_row(
            project_id=project_id,
            context_id=context_id,
            task_id=task_id,
        )
        if row is None:
            raise ValueError(f"Task {task_id} not found.")

        binding = None
        if is_provider_event_task_row(row.data):
            binding = self._provider_trigger_dao.get_binding_for_task(
                project_id=project_id,
                source_task_log_id=row.log_event_id,
                for_update=True,
            )

        locked_row = self._get_latest_task_row(
            project_id=project_id,
            context_id=context_id,
            task_id=task_id,
            for_update=True,
        )
        if locked_row is None:
            raise ValueError(f"Task {task_id} not found.")
        row = locked_row

        if row.task_revision != expected_task_revision:
            raise TaskRevisionConflict(latest_revision=row.task_revision)

        if is_provider_event_task_row(row.data):
            try:
                classification = classify_provider_event_update_fields(
                    updates,
                    existing_data=row.data,
                )
            except ProviderEventWriteRejected as exc:
                raise ValueError(exc.reason) from exc
            if classification is not ProviderEventUpdateKind.authored:
                raise ValueError("provider_event_authored_mutation_required")

        merged = dict(row.data)
        merged.update(updates)
        if "trigger" in updates:
            updated_trigger = parse_task_trigger(merged.get("trigger"))
            if isinstance(updated_trigger, ProviderEventTrigger):
                validate_provider_event_trigger_for_assistant(
                    self.session,
                    assistant_id=int(assistant.agent_id),
                    trigger=updated_trigger,
                )
        merged[TaskRowKey.task_revision.value] = row.task_revision + 1
        log_event = self._lock_task_row(
            project_id=project_id,
            log_event_id=row.log_event_id,
        )
        _replace_log_payload(log_event, merged)
        self.session.flush()

        if is_provider_event_task_row(merged):
            self._sync_provider_event_binding(
                project_id=project_id,
                data=merged,
                source_task_log_id=row.log_event_id,
                task_revision=int(merged[TaskRowKey.task_revision.value]),
                bump_acceptance_epoch=bump_acceptance_epoch,
                binding=binding,
            )

        self._project_task_executions(
            project_id=project_id,
            tasks_context_name=tasks_context_name,
            task_ids={task_id},
        )
        return TaskMutationResult(
            log_event_id=row.log_event_id,
            task_id=task_id,
            task_revision=int(merged[TaskRowKey.task_revision.value]),
            data=merged,
        )

    def pause_provider_trigger(
        self,
        *,
        assistant: Assistant,
        task_id: int,
        expected_task_revision: int,
    ) -> TaskMutationResult:
        """Pause provider-event automation without disabling manual execution."""

        row = self.get_task(assistant=assistant, task_id=task_id)
        if row is None:
            raise ValueError(f"Task {task_id} not found.")
        trigger = row.data.get("trigger")
        if not isinstance(trigger, dict):
            raise ValueError("Task has no provider-event trigger.")
        updated_trigger = {**trigger, "state": "paused"}
        return self.mutate_authored_task(
            assistant=assistant,
            task_id=task_id,
            expected_task_revision=expected_task_revision,
            updates={"trigger": updated_trigger},
            bump_acceptance_epoch=True,
        )

    def resume_provider_trigger(
        self,
        *,
        assistant: Assistant,
        task_id: int,
        expected_task_revision: int,
    ) -> TaskMutationResult:
        """Resume provider-event automation."""

        row = self.get_task(assistant=assistant, task_id=task_id)
        if row is None:
            raise ValueError(f"Task {task_id} not found.")
        trigger = row.data.get("trigger")
        if not isinstance(trigger, dict):
            raise ValueError("Task has no provider-event trigger.")
        updated_trigger = {**trigger, "state": "enabled"}
        return self.mutate_authored_task(
            assistant=assistant,
            task_id=task_id,
            expected_task_revision=expected_task_revision,
            updates={"trigger": updated_trigger},
            bump_acceptance_epoch=True,
        )

    def retry_provider_trigger(
        self,
        *,
        assistant: Assistant,
        task_id: int,
    ) -> None:
        """Request immediate reconciliation for one provider-event binding."""

        binding = self.get_binding_for_task(
            assistant=assistant,
            task_id=task_id,
            for_update=True,
        )
        if binding is None:
            raise ValueError(f"Task {task_id} has no provider-event binding.")
        self._provider_trigger_dao.request_reconcile(binding=binding)

    def delete_task(
        self,
        *,
        assistant: Assistant,
        task_id: int,
        expected_task_revision: int,
    ) -> TaskMutationResult:
        """Delete the current authored row under a revision CAS."""

        project_id, context_id, tasks_context_name = self._resolve_task_scope(assistant)
        row_preview = self._get_latest_task_row(
            project_id=project_id,
            context_id=context_id,
            task_id=task_id,
        )
        if row_preview is None:
            raise ValueError(f"Task {task_id} not found.")

        if is_provider_event_task_row(row_preview.data):
            binding = self._provider_trigger_dao.get_binding_for_task(
                project_id=project_id,
                source_task_log_id=row_preview.log_event_id,
                for_update=True,
            )
            if binding is not None:
                self._provider_trigger_dao.tombstone_binding(binding=binding)

        row = self._get_latest_task_row(
            project_id=project_id,
            context_id=context_id,
            task_id=task_id,
            for_update=True,
        )
        if row is None:
            raise ValueError(f"Task {task_id} not found.")
        if row.task_revision != expected_task_revision:
            raise TaskRevisionConflict(latest_revision=row.task_revision)

        log_event = self._lock_task_row(
            project_id=project_id,
            log_event_id=row.log_event_id,
        )
        # FKs from lec/luc/embedding were dropped for partitioning — clean
        # children explicitly (same shape as task-machine row deletes).
        self.session.execute(
            delete(LogUniqueConstraint).where(
                LogUniqueConstraint.log_event_id == log_event.id,
                LogUniqueConstraint.context_id == context_id,
                LogUniqueConstraint.project_id == project_id,
            ),
        )
        self.session.execute(
            delete(LogEventContext).where(
                LogEventContext.project_id == project_id,
                LogEventContext.log_event_id == log_event.id,
                LogEventContext.context_id == context_id,
            ),
        )
        delete_orphaned_log_events(
            session=self.session,
            project_id=project_id,
            skip_embedding_cleanup=True,
            log_event_ids=[log_event.id],
        )
        self.session.flush()
        self._project_task_executions(
            project_id=project_id,
            tasks_context_name=tasks_context_name,
            task_ids={task_id},
        )
        return TaskMutationResult(
            log_event_id=row.log_event_id,
            task_id=task_id,
            task_revision=row.task_revision + 1,
            data=row.data,
        )

    def _resolve_task_scope(
        self,
        assistant: Assistant,
    ) -> tuple[int, int, str]:
        """Resolve Assistants project, Tasks context, and context name."""

        if not assistant.user_id:
            raise ValueError("Assistant is missing an owning user_id.")
        project = self._project_dao.get_by_user_and_name(
            user_id=str(assistant.user_id),
            name=TASK_MACHINE_PROJECT_NAME,
        )
        if project is None:
            raise ValueError("Assistants project not found for assistant owner.")
        tasks_context_name = resolve_tasks_context_name(
            self.session,
            project.id,
            assistant_id=str(assistant.agent_id),
        )
        context_id = self._context_dao.get_or_create(
            project.id,
            name=tasks_context_name,
            description=None,
            is_versioned=False,
        )
        return project.id, context_id, tasks_context_name

    def _allocate_task_id(self, *, project_id: int, context_id: int) -> int:
        """Return the next logical task id for one Tasks context."""

        owner_key_filter = single_owner_key_for_context(self.session, context_id)
        rows = (
            self.session.query(LogEvent.data)
            .join(LogEventContext, log_event_context_join(owner_key=owner_key_filter))
            .filter(
                LogEvent.project_id == project_id,
                LogEventContext.project_id == project_id,
                owner_scope_clause(LogEvent, owner_key_filter),
                owner_scope_clause(LogEventContext, owner_key_filter),
                LogEventContext.context_id == context_id,
            )
            .all()
        )
        max_task_id = 0
        for (data,) in rows:
            if isinstance(data, dict):
                task_id = _coerce_int(data.get("task_id"))
                if task_id is not None:
                    max_task_id = max(max_task_id, task_id)
        return max_task_id + 1

    def _load_task_rows(
        self,
        *,
        project_id: int,
        context_id: int,
        task_id: int | None,
        limit: int = 100,
        for_update: bool = False,
    ) -> list[TaskRowSnapshot]:
        owner_key_filter = single_owner_key_for_context(self.session, context_id)
        query = (
            self.session.query(LogEvent)
            .join(LogEventContext, log_event_context_join(owner_key=owner_key_filter))
            .filter(
                LogEvent.project_id == project_id,
                LogEventContext.project_id == project_id,
                owner_scope_clause(LogEvent, owner_key_filter),
                owner_scope_clause(LogEventContext, owner_key_filter),
                LogEventContext.context_id == context_id,
            )
            .order_by(LogEvent.id.desc())
            .limit(limit)
        )
        if for_update:
            query = query.with_for_update()
        snapshots: list[TaskRowSnapshot] = []
        for log_event in query.all():
            data = dict(log_event.data or {})
            logical_task_id = _coerce_int(data.get("task_id"))
            if logical_task_id is None:
                continue
            if task_id is not None and logical_task_id != task_id:
                continue
            snapshots.append(
                TaskRowSnapshot(
                    log_event_id=log_event.id,
                    task_id=logical_task_id,
                    data=data,
                    task_revision=current_task_revision(data),
                ),
            )
        return snapshots

    def _get_latest_task_row(
        self,
        *,
        project_id: int,
        context_id: int,
        task_id: int,
        for_update: bool = False,
    ) -> TaskRowSnapshot | None:
        rows = self._load_task_rows(
            project_id=project_id,
            context_id=context_id,
            task_id=task_id,
            limit=50,
            for_update=for_update,
        )
        if not rows:
            return None
        return max(rows, key=lambda row: row.log_event_id)

    def _lock_task_row(self, *, project_id: int, log_event_id: int) -> LogEvent:
        log_event = (
            self.session.query(LogEvent)
            .filter(
                LogEvent.project_id == project_id,
                LogEvent.id == log_event_id,
            )
            .with_for_update()
            .one()
        )
        return log_event

    def _sync_provider_event_binding(
        self,
        *,
        project_id: int,
        data: dict[str, Any],
        source_task_log_id: int,
        task_revision: int,
        bump_acceptance_epoch: bool,
        binding=None,
    ) -> None:
        binding_id = data.get(TaskRowKey.provider_event_binding_id.value)
        if not binding_id:
            return
        trigger = parse_task_trigger(data.get("trigger"))
        if not isinstance(trigger, ProviderEventTrigger):
            return
        if binding is None:
            binding = self._provider_trigger_dao.get_binding_for_task(
                project_id=project_id,
                source_task_log_id=source_task_log_id,
                for_update=True,
            )
        if binding is None:
            return
        execution_mode = "offline" if _coerce_bool(data.get("offline")) else "live"
        self._provider_trigger_dao.sync_desired_state(
            binding=binding,
            task_revision=task_revision,
            trigger=trigger,
            execution_mode=execution_mode,
            entrypoint=_coerce_int(data.get("entrypoint")),
            bump_acceptance_epoch=bump_acceptance_epoch,
            task_enabled=_coerce_bool(data.get("enabled", True)),
            requires_filesystem=_requires_filesystem_from_row(data),
            requires_computer=_requires_computer_from_row(data),
        )

    def _project_task_executions(
        self,
        *,
        project_id: int,
        tasks_context_name: str,
        task_ids: set[int],
    ) -> None:
        sync_task_executions_for_task_ids(
            self.session,
            project_id,
            task_ids,
            tasks_context_name=tasks_context_name,
        )
