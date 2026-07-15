"""Task revision CAS and acceptance ordering for provider-event triggers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy.orm import Session

from orchestra.db.dao.provider_trigger_dao import ProviderTriggerDAO
from orchestra.db.models.provider_trigger_models import EventTriggerBinding
from orchestra.provider_triggers.runtime_types import DesiredTriggerState
from orchestra.provider_triggers.task_trigger import ProviderEventTrigger


class TaskRevisionConflict(Exception):
    """Raised when an authored provider-trigger write loses a revision CAS."""

    def __init__(self, *, latest_revision: int) -> None:
        self.latest_revision = latest_revision
        super().__init__(f"task_revision_conflict:{latest_revision}")


class AcceptanceRejected(Exception):
    """Raised when event acceptance loses against a lifecycle fence."""

    def __init__(self, *, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class MutationResult:
    """Outcome of one authored provider-trigger mutation."""

    binding_id: str
    task_revision: int
    acceptance_epoch: int
    desired_state: str
    acceptance_open: bool


def _dao(session: Session) -> ProviderTriggerDAO:
    return ProviderTriggerDAO(session)


def _require_binding(session: Session, *, binding_id: str) -> EventTriggerBinding:
    binding = _dao(session).get_binding(binding_id=binding_id, for_update=True)
    if binding is None:
        raise ValueError(f"Binding {binding_id} not found.")
    return binding


def _mutation_result(binding: EventTriggerBinding) -> MutationResult:
    return MutationResult(
        binding_id=binding.binding_id,
        task_revision=binding.task_revision,
        acceptance_epoch=binding.acceptance_epoch,
        desired_state=binding.desired_trigger_state,
        acceptance_open=binding.local_acceptance_open,
    )


def mutate_provider_trigger_task(
    session: Session,
    *,
    binding_id: str,
    expected_task_revision: int,
    desired_state: str,
    open_acceptance: bool,
    write_origin: Literal["typed", "unity"] = "typed",
) -> MutationResult:
    """Apply one authored provider-trigger mutation under a revision CAS."""

    del write_origin
    binding = _require_binding(session, binding_id=binding_id)
    if binding.task_revision != expected_task_revision:
        raise TaskRevisionConflict(latest_revision=binding.task_revision)

    trigger = ProviderEventTrigger(
        state=desired_state,  # type: ignore[arg-type]
        connection_id=binding.connection_id,
        backend_id=binding.backend_id,
        canonical_app_slug=binding.canonical_app_slug,
        event_slug=binding.event_slug,
        schema_version=binding.schema_version,
        filters=[],
    )
    _dao(session).sync_desired_state(
        binding=binding,
        task_revision=binding.task_revision + 1,
        trigger=trigger,
        execution_mode=binding.execution_mode,
        entrypoint=binding.entrypoint,
        bump_acceptance_epoch=True,
    )
    if open_acceptance and desired_state == DesiredTriggerState.enabled.value:
        binding.local_acceptance_open = True
        session.flush()
    return _mutation_result(binding)


def pause_provider_trigger(session: Session, *, binding_id: str) -> MutationResult:
    """Close acceptance while keeping the task authored and saved."""

    binding = _require_binding(session, binding_id=binding_id)
    trigger = ProviderEventTrigger(
        state="paused",
        connection_id=binding.connection_id,
        backend_id=binding.backend_id,
        canonical_app_slug=binding.canonical_app_slug,
        event_slug=binding.event_slug,
        schema_version=binding.schema_version,
        filters=[],
    )
    _dao(session).sync_desired_state(
        binding=binding,
        task_revision=binding.task_revision,
        trigger=trigger,
        execution_mode=binding.execution_mode,
        entrypoint=binding.entrypoint,
        bump_acceptance_epoch=True,
    )
    return _mutation_result(binding)


def promote_active_generation(session: Session, *, binding_id: str) -> MutationResult:
    """Open acceptance after a promoted provider subscription generation."""

    dao = _dao(session)
    binding = _require_binding(session, binding_id=binding_id)
    generation = dao.create_generation(binding=binding)
    dao.promote_generation(binding=binding, generation=generation)
    return _mutation_result(binding)


def sync_fence_after_task_row_mutation(
    session: Session,
    *,
    binding_id: str,
    task_revision: int,
    desired_state: str,
    open_acceptance: bool,
    bump_acceptance_epoch: bool,
) -> MutationResult:
    """Mirror one authored task-row revision onto the derived binding."""

    binding = _require_binding(session, binding_id=binding_id)
    trigger = ProviderEventTrigger(
        state=desired_state,  # type: ignore[arg-type]
        connection_id=binding.connection_id,
        backend_id=binding.backend_id,
        canonical_app_slug=binding.canonical_app_slug,
        event_slug=binding.event_slug,
        schema_version=binding.schema_version,
        filters=[],
    )
    _dao(session).sync_desired_state(
        binding=binding,
        task_revision=task_revision,
        trigger=trigger,
        execution_mode=binding.execution_mode,
        entrypoint=binding.entrypoint,
        bump_acceptance_epoch=bump_acceptance_epoch,
    )
    if open_acceptance:
        binding.local_acceptance_open = True
        session.flush()
    return _mutation_result(binding)
