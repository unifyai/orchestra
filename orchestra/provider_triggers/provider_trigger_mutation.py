"""Task revision CAS and acceptance-fence for provider-event triggers."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import Boolean, Integer, String, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column


class ProviderTriggerDeclarativeBase(DeclarativeBase):
    """Declarative base for provider-trigger fence tables."""


class ProviderTriggerBindingFence(ProviderTriggerDeclarativeBase):
    """Derived binding fence used for CAS and lifecycle ordering."""

    __tablename__ = "provider_trigger_binding_fence"

    binding_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    acceptance_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    acceptance_open: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )
    desired_state: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="draft",
    )
    accepted_receipt_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )


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


@dataclass(frozen=True)
class AcceptanceResult:
    """Outcome of one transactional acceptance attempt."""

    binding_id: str
    accepted: bool
    receipt_id: str | None
    acceptance_epoch: int


def ensure_binding_fence_schema(session: Session) -> None:
    """Create binding-fence tables when running against a fresh test database."""

    bind = session.get_bind()
    ProviderTriggerDeclarativeBase.metadata.create_all(bind)


def initialize_binding(
    session: Session,
    *,
    binding_id: str | None = None,
    desired_state: str = "enabled",
) -> MutationResult:
    """Create a new derived binding fence."""

    ensure_binding_fence_schema(session)
    resolved_binding_id = binding_id or f"binding-{uuid.uuid4().hex[:12]}"
    fence = ProviderTriggerBindingFence(
        binding_id=resolved_binding_id,
        task_revision=1,
        acceptance_epoch=1,
        acceptance_open=False,
        desired_state=desired_state,
        accepted_receipt_count=0,
    )
    session.add(fence)
    session.flush()
    return MutationResult(
        binding_id=resolved_binding_id,
        task_revision=1,
        acceptance_epoch=1,
        desired_state=desired_state,
        acceptance_open=False,
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

    del write_origin  # typed and Unity write seams share this service
    ensure_binding_fence_schema(session)
    fence = session.execute(
        select(ProviderTriggerBindingFence)
        .where(ProviderTriggerBindingFence.binding_id == binding_id)
        .with_for_update(),
    ).scalar_one()
    if fence.task_revision != expected_task_revision:
        raise TaskRevisionConflict(latest_revision=fence.task_revision)

    fence.task_revision += 1
    fence.acceptance_epoch += 1
    fence.desired_state = desired_state
    fence.acceptance_open = open_acceptance
    session.flush()
    return MutationResult(
        binding_id=fence.binding_id,
        task_revision=fence.task_revision,
        acceptance_epoch=fence.acceptance_epoch,
        desired_state=fence.desired_state,
        acceptance_open=fence.acceptance_open,
    )


def pause_provider_trigger(session: Session, *, binding_id: str) -> MutationResult:
    """Close acceptance while keeping the task authored and saved."""

    ensure_binding_fence_schema(session)
    fence = session.execute(
        select(ProviderTriggerBindingFence)
        .where(ProviderTriggerBindingFence.binding_id == binding_id)
        .with_for_update(),
    ).scalar_one()
    fence.acceptance_epoch += 1
    fence.desired_state = "paused"
    fence.acceptance_open = False
    session.flush()
    return MutationResult(
        binding_id=fence.binding_id,
        task_revision=fence.task_revision,
        acceptance_epoch=fence.acceptance_epoch,
        desired_state=fence.desired_state,
        acceptance_open=fence.acceptance_open,
    )


def promote_active_generation(session: Session, *, binding_id: str) -> MutationResult:
    """Open acceptance after a promoted provider subscription generation."""

    ensure_binding_fence_schema(session)
    fence = session.execute(
        select(ProviderTriggerBindingFence)
        .where(ProviderTriggerBindingFence.binding_id == binding_id)
        .with_for_update(),
    ).scalar_one()
    fence.desired_state = "enabled"
    fence.acceptance_open = True
    session.flush()
    return MutationResult(
        binding_id=fence.binding_id,
        task_revision=fence.task_revision,
        acceptance_epoch=fence.acceptance_epoch,
        desired_state=fence.desired_state,
        acceptance_open=fence.acceptance_open,
    )


def attempt_event_acceptance(
    session: Session,
    *,
    binding_id: str,
    acceptance_epoch: int,
) -> AcceptanceResult:
    """Try to accept one provider event under the shared binding lock."""

    ensure_binding_fence_schema(session)
    fence = session.execute(
        select(ProviderTriggerBindingFence)
        .where(ProviderTriggerBindingFence.binding_id == binding_id)
        .with_for_update(),
    ).scalar_one()
    if not fence.acceptance_open or fence.desired_state != "enabled":
        raise AcceptanceRejected(reason="inactive_trigger")
    if fence.acceptance_epoch != acceptance_epoch:
        raise AcceptanceRejected(reason="stale_acceptance_epoch")

    fence.accepted_receipt_count += 1
    receipt_id = f"receipt-{fence.accepted_receipt_count}"
    session.flush()
    return AcceptanceResult(
        binding_id=fence.binding_id,
        accepted=True,
        receipt_id=receipt_id,
        acceptance_epoch=fence.acceptance_epoch,
    )
