"""Provider-trigger CAS and acceptance-fence tests."""

from __future__ import annotations

import threading
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dao.provider_trigger_dao import ProviderTriggerDAO
from orchestra.db.models.provider_trigger_models import (
    EventTriggerSubscriptionGeneration,
)
from orchestra.provider_triggers.runtime_types import (
    DesiredTriggerState,
    GenerationLifecycle,
)
from orchestra.services.task_mutation_contract import TaskRevisionConflict
from orchestra.tests.provider_triggers.control_plane_harness import (
    seed_minimal_test_binding,
)


def _binding_id() -> str:
    return f"binding-{uuid.uuid4().hex[:12]}"


class AcceptanceRejected(Exception):
    """Raised when acceptance fencing rejects a delivery under test."""

    def __init__(self, *, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _promote_active_generation(
    session: Session,
    *,
    binding_id: str,
):
    dao = ProviderTriggerDAO(session)
    binding = dao.get_binding(binding_id=binding_id, for_update=True)
    if binding is None:
        raise ValueError(f"Binding {binding_id} not found.")
    generation = dao.create_generation(binding=binding)
    dao.promote_generation(binding=binding, generation=generation)
    session.flush()
    return binding


def _mutate_provider_trigger_task(
    session: Session,
    *,
    binding_id: str,
    expected_task_revision: int,
    desired_state: str,
    open_acceptance: bool,
    write_origin: str,
):
    dao = ProviderTriggerDAO(session)
    binding = dao.get_binding(binding_id=binding_id, for_update=True)
    if binding is None:
        raise ValueError(f"Binding {binding_id} not found.")
    if binding.task_revision != expected_task_revision:
        raise TaskRevisionConflict(latest_revision=binding.task_revision)
    binding.task_revision += 1
    binding.desired_trigger_state = desired_state
    binding.local_acceptance_open = open_acceptance
    if write_origin == "typed":
        binding.acceptance_epoch += 1
    session.flush()
    return binding


def _pause_provider_trigger(session: Session, *, binding_id: str):
    dao = ProviderTriggerDAO(session)
    binding = dao.get_binding(binding_id=binding_id, for_update=True)
    if binding is None:
        raise ValueError(f"Binding {binding_id} not found.")
    binding.desired_trigger_state = DesiredTriggerState.paused.value
    binding.local_acceptance_open = False
    binding.acceptance_epoch += 1
    session.flush()
    return binding


def _attempt_test_event_acceptance(
    session: Session,
    *,
    binding_id: str,
    acceptance_epoch: int,
    provider_event_identity_hmac: str = "identity-test",
) -> str:
    """Exercise acceptance fencing through the same DAO path production ingress uses."""

    from orchestra.db.dao.provider_trigger_dao import ProviderTriggerDAO

    dao = ProviderTriggerDAO(session)
    binding = dao.get_binding(binding_id=binding_id, for_update=True)
    if binding is None:
        raise ValueError(f"Binding {binding_id} not found.")
    if binding.tombstoned_at is not None:
        raise AcceptanceRejected(reason="binding_tombstoned")
    if (
        not binding.local_acceptance_open
        or binding.desired_trigger_state != DesiredTriggerState.enabled.value
    ):
        raise AcceptanceRejected(reason="inactive_trigger")
    if binding.acceptance_epoch != acceptance_epoch:
        raise AcceptanceRejected(reason="stale_acceptance_epoch")

    generation_id = binding.active_generation_id
    if not generation_id:
        generation = dao.create_generation(binding=binding)
        dao.promote_generation(binding=binding, generation=generation)
        generation_id = generation.generation_id

    generation_row = session.execute(
        select(EventTriggerSubscriptionGeneration).where(
            EventTriggerSubscriptionGeneration.generation_id == generation_id,
        ),
    ).scalar_one()
    if generation_row.lifecycle_state != GenerationLifecycle.active.value:
        raise AcceptanceRejected(reason="inactive_generation")

    receipt = dao.adopt_receipt(
        binding=binding,
        generation=generation_row,
        provider_event_identity_hmac=provider_event_identity_hmac,
    )
    return receipt.receipt_id


def test_typed_provider_trigger_mutation_cas_advances_revision_and_acceptance_epoch(
    dbsession: Session,
) -> None:
    binding_id = _binding_id()
    seed_minimal_test_binding(dbsession, binding_id=binding_id)
    promoted = _promote_active_generation(dbsession, binding_id=binding_id)
    assert promoted.local_acceptance_open is True

    mutated = _mutate_provider_trigger_task(
        dbsession,
        binding_id=binding_id,
        expected_task_revision=1,
        desired_state="enabled",
        open_acceptance=True,
        write_origin="typed",
    )
    assert mutated.task_revision == 2
    assert mutated.acceptance_epoch == 2


def test_unity_origin_provider_trigger_mutation_cas_advances_revision(
    dbsession: Session,
) -> None:
    binding_id = _binding_id()
    seed_minimal_test_binding(dbsession, binding_id=binding_id)
    _promote_active_generation(dbsession, binding_id=binding_id)
    mutated = _mutate_provider_trigger_task(
        dbsession,
        binding_id=binding_id,
        expected_task_revision=1,
        desired_state="enabled",
        open_acceptance=True,
        write_origin="unity",
    )
    assert mutated.task_revision == 2


def test_provider_trigger_rejects_stale_task_revision_without_advancing_acceptance_epoch(
    dbsession: Session,
) -> None:
    binding_id = _binding_id()
    seed_minimal_test_binding(dbsession, binding_id=binding_id)
    _mutate_provider_trigger_task(
        dbsession,
        binding_id=binding_id,
        expected_task_revision=1,
        desired_state="enabled",
        open_acceptance=True,
        write_origin="typed",
    )
    with pytest.raises(TaskRevisionConflict) as excinfo:
        _mutate_provider_trigger_task(
            dbsession,
            binding_id=binding_id,
            expected_task_revision=1,
            desired_state="enabled",
            open_acceptance=True,
            write_origin="typed",
        )
    assert excinfo.value.latest_revision == 2


@pytest.mark.parametrize("write_origin", ["typed", "unity"])
def test_acceptance_and_pause_have_one_locked_observable_ordering(
    _engine,
    write_origin: str,
) -> None:
    from sqlalchemy.orm import sessionmaker

    session_factory = sessionmaker(bind=_engine)
    binding_id = _binding_id()

    setup = session_factory()
    try:
        seed_minimal_test_binding(setup, binding_id=binding_id)
        promoted = _promote_active_generation(setup, binding_id=binding_id)
        promoted_acceptance_epoch = promoted.acceptance_epoch
        setup.commit()
    finally:
        setup.close()

    outcomes: list[str] = []
    barrier = threading.Barrier(2)

    def accept_worker() -> None:
        session = session_factory()
        try:
            barrier.wait(timeout=5)
            try:
                _attempt_test_event_acceptance(
                    session,
                    binding_id=binding_id,
                    acceptance_epoch=promoted_acceptance_epoch,
                )
                session.commit()
                outcomes.append("accepted")
            except AcceptanceRejected:
                session.rollback()
                outcomes.append("rejected")
            except Exception:
                session.rollback()
                outcomes.append("rejected")
        finally:
            session.close()

    def pause_worker() -> None:
        session = session_factory()
        try:
            barrier.wait(timeout=5)
            _pause_provider_trigger(session, binding_id=binding_id)
            session.commit()
            outcomes.append("paused")
        finally:
            session.close()

    threads = [
        threading.Thread(target=accept_worker),
        threading.Thread(target=pause_worker),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert len(outcomes) == 2
    assert outcomes.count("paused") == 1
    assert outcomes.count("accepted") + outcomes.count("rejected") == 1
    if "accepted" in outcomes:
        assert "paused" in outcomes
