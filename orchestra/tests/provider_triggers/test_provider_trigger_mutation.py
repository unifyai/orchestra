"""Provider-trigger CAS and acceptance-fence tests."""

from __future__ import annotations

import threading
import uuid

import pytest
from sqlalchemy.orm import Session

from orchestra.provider_triggers.provider_trigger_mutation import (
    AcceptanceRejected,
    TaskRevisionConflict,
    attempt_event_acceptance,
    initialize_binding,
    mutate_provider_trigger_task,
    pause_provider_trigger,
    promote_active_generation,
)


def _binding_id() -> str:
    return f"binding-{uuid.uuid4().hex[:12]}"


def test_typed_provider_trigger_accepts_matching_task_revision_and_projects_runtime_state(
    dbsession: Session,
) -> None:
    binding_id = _binding_id()
    initialize_binding(dbsession, binding_id=binding_id)
    promoted = promote_active_generation(dbsession, binding_id=binding_id)
    assert promoted.acceptance_open is True

    mutated = mutate_provider_trigger_task(
        dbsession,
        binding_id=binding_id,
        expected_task_revision=1,
        desired_state="enabled",
        open_acceptance=True,
        write_origin="typed",
    )
    assert mutated.task_revision == 2
    assert mutated.acceptance_epoch == 2

    accepted = attempt_event_acceptance(
        dbsession,
        binding_id=binding_id,
        acceptance_epoch=mutated.acceptance_epoch,
    )
    assert accepted.accepted is True
    assert accepted.receipt_id == "receipt-1"


def test_unity_provider_trigger_accepts_matching_task_revision_and_projects_runtime_state(
    dbsession: Session,
) -> None:
    binding_id = _binding_id()
    initialize_binding(dbsession, binding_id=binding_id)
    promote_active_generation(dbsession, binding_id=binding_id)
    mutated = mutate_provider_trigger_task(
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
    initialize_binding(dbsession, binding_id=binding_id)
    mutate_provider_trigger_task(
        dbsession,
        binding_id=binding_id,
        expected_task_revision=1,
        desired_state="enabled",
        open_acceptance=True,
        write_origin="typed",
    )
    with pytest.raises(TaskRevisionConflict) as excinfo:
        mutate_provider_trigger_task(
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
        initialize_binding(setup, binding_id=binding_id)
        promoted = promote_active_generation(setup, binding_id=binding_id)
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
                attempt_event_acceptance(
                    session,
                    binding_id=binding_id,
                    acceptance_epoch=promoted.acceptance_epoch,
                )
                session.commit()
                outcomes.append("accepted")
            except AcceptanceRejected:
                session.rollback()
                outcomes.append("rejected")
        finally:
            session.close()

    def pause_worker() -> None:
        session = session_factory()
        try:
            barrier.wait(timeout=5)
            pause_provider_trigger(session, binding_id=binding_id)
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
