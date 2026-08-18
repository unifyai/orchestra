"""Production-path tests for Orchestra-authoritative dispatch adoption."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.orm import Session

from orchestra.db.dao.provider_trigger_dao import ProviderTriggerDAO
from orchestra.db.models.core_models import Project
from orchestra.db.models.orchestra_models import Assistant
from orchestra.db.models.provider_trigger_models import ProviderEventDispatch
from orchestra.provider_triggers.dispatch_request import (
    COMMUNICATION_DISPATCH_AUDIENCE,
    UNIFY_DISPATCH_AUDIENCE,
)
from orchestra.provider_triggers.runtime_types import (
    DispatchProcessingState,
    DownstreamAdoptionStatus,
)
from orchestra.provider_triggers.task_trigger import ProviderEventTrigger
from orchestra.services.provider_event_dispatch_adoption_service import (
    DispatchAdoptionError,
    DispatchAuthorizationSnapshot,
    ProviderEventDispatchAdoptionService,
)
from orchestra.services.provider_event_dispatch_delivery_service import (
    ProviderEventDispatchDeliveryService,
)
from orchestra.services.task_machine_state_service import (
    TASK_MACHINE_PROJECT_NAME,
    create_task_run_if_absent,
    update_task_run,
)
from orchestra.tests.test_log import HEADERS, HEADERS_2
from orchestra.tests.utils import ADMIN_HEADERS

PRIMARY_USER_ID = str(os.getenv("AUTH_ACCOUNT_USER_ID"))
CLAIM_PATH = "/v0/admin/provider-event-dispatch/claim"
REPORT_STARTED_PATH = "/v0/admin/provider-event-dispatch/report-started"
REPORT_TERMINAL_PATH = "/v0/admin/provider-event-dispatch/report-terminal"
USER_CLAIM_PATH = "/v0/provider-event-dispatch/claim"


class _DispatchFixture:
    def __init__(
        self,
        *,
        assistant_id: int,
        task_id: int,
        run_id: int,
        run_key: str,
        binding_id: str,
        receipt_id: str,
        operation_id: str,
        accepted_revision: str,
        dispatch_mode: str,
        audience: str,
        project_id: int,
    ) -> None:
        self.assistant_id = assistant_id
        self.task_id = task_id
        self.run_id = run_id
        self.run_key = run_key
        self.binding_id = binding_id
        self.receipt_id = receipt_id
        self.operation_id = operation_id
        self.accepted_revision = accepted_revision
        self.dispatch_mode = dispatch_mode
        self.audience = audience
        self.project_id = project_id


def _seed_dispatch(
    dbsession: Session,
    *,
    dispatch_mode: str = "live",
    audience: str = UNIFY_DISPATCH_AUDIENCE,
) -> _DispatchFixture:
    assistant = Assistant(
        user_id=PRIMARY_USER_ID,
        first_name="Adoption",
        surname="Bot",
    )
    dbsession.add(assistant)
    dbsession.flush()

    project = (
        dbsession.query(Project)
        .filter_by(user_id=PRIMARY_USER_ID, name=TASK_MACHINE_PROJECT_NAME)
        .one_or_none()
    )
    if project is None:
        project = Project(name=TASK_MACHINE_PROJECT_NAME, user_id=PRIMARY_USER_ID)
        dbsession.add(project)
        dbsession.flush()

    task_id = int(uuid.uuid4().int % 1_000_000) + 1
    receipt_id = f"receipt-{uuid.uuid4().hex[:12]}"
    run_key = f"provider-event-run-{uuid.uuid4().hex}"
    run, _ = create_task_run_if_absent(
        dbsession,
        project.id,
        {
            "run_key": run_key,
            "assistant_id": str(assistant.agent_id),
            "task_id": task_id,
            "wake": "provider_event",
            "delivery": dispatch_mode,
            "state": "pending",
            "provider_event_receipt_id": receipt_id,
        },
    )

    dao = ProviderTriggerDAO(dbsession)
    trigger = ProviderEventTrigger(
        state="enabled",
        connection_id="conn-adoption",
        backend_id="composio",
        canonical_app_slug="github",
        provider_trigger_slug="GITHUB_ISSUE_CREATED_TRIGGER",
        trigger_config={},
    )
    binding = dao.create_binding(
        binding_id=f"binding-{uuid.uuid4().hex[:12]}",
        project_id=project.id,
        tasks_context_id=0,
        source_task_log_id=run.id,
        task_id=task_id,
        assistant_id=assistant.agent_id,
        task_revision=1,
        trigger=trigger,
        execution_mode=dispatch_mode,
        entrypoint=None,
    )
    generation = dao.create_generation(binding=binding)
    receipt = dao.adopt_receipt(
        binding=binding,
        generation=generation,
        provider_event_identity_hmac=f"event-{uuid.uuid4().hex}",
        receipt_id=receipt_id,
    )
    receipt.run_id = run.id
    receipt.run_key = run_key
    receipt.event_context_ref = f"blob://{binding.binding_id}/{receipt_id}"
    receipt.accepted_revision = binding.desired_revision
    dispatch = dao.adopt_dispatch(
        receipt=receipt,
        binding=binding,
        run_id=run.id,
        run_key=run_key,
        audience=audience,
    )
    dbsession.flush()

    return _DispatchFixture(
        assistant_id=assistant.agent_id,
        task_id=task_id,
        run_id=run.id,
        run_key=run_key,
        binding_id=binding.binding_id,
        receipt_id=receipt.receipt_id,
        operation_id=dispatch.operation_id,
        accepted_revision=dispatch.accepted_revision,
        dispatch_mode=dispatch_mode,
        audience=audience,
        project_id=project.id,
    )


def _authorization(fixture: _DispatchFixture) -> DispatchAuthorizationSnapshot:
    return DispatchAuthorizationSnapshot(
        operation_id=fixture.operation_id,
        run_id=fixture.run_id,
        run_key=fixture.run_key,
        assistant_id=fixture.assistant_id,
        task_id=fixture.task_id,
        binding_id=fixture.binding_id,
        receipt_id=fixture.receipt_id,
        accepted_revision=fixture.accepted_revision,
        dispatch_mode=fixture.dispatch_mode,
        audience=fixture.audience,
    )


def _claim_payload(fixture: _DispatchFixture, *, claimant_id: str) -> dict:
    return {
        "operation_id": fixture.operation_id,
        "run_id": fixture.run_id,
        "run_key": fixture.run_key,
        "assistant_id": str(fixture.assistant_id),
        "task_id": fixture.task_id,
        "binding_id": fixture.binding_id,
        "receipt_id": fixture.receipt_id,
        "accepted_revision": fixture.accepted_revision,
        "dispatch_mode": fixture.dispatch_mode,
        "audience": fixture.audience,
        "claimant_id": claimant_id,
        "launch_identity": f"provider_event_operation:{fixture.operation_id}",
    }


def test_concurrent_claims_grant_exactly_one_launch_owner(_engine) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from sqlalchemy.orm import sessionmaker

    factory = sessionmaker(bind=_engine)
    setup = factory()
    try:
        fixture = _seed_dispatch(setup)
        setup.commit()
    finally:
        setup.close()

    authorization = _authorization(fixture)

    def _claim(claimant_id: str):
        session = factory()
        try:
            # Escape AUTOCOMMIT so FOR UPDATE holds until commit.
            session.connection(
                execution_options={"isolation_level": "READ COMMITTED"},
            )
            service = ProviderEventDispatchAdoptionService(session)
            result = service.claim(
                authorization=authorization,
                claimant_id=claimant_id,
                launch_identity=f"provider_event_operation:{fixture.operation_id}",
            )
            session.commit()
            return result
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(_claim, f"rail-{index}") for index in range(4)]
        results = [future.result() for future in futures]

    owners = [result for result in results if result.owns_launch]
    non_owners = [result for result in results if not result.owns_launch]
    assert len(owners) == 1
    assert len(non_owners) == 3
    assert {result.fencing_token for result in results} == {
        owners[0].fencing_token,
    }
    assert owners[0].launch_identity == (
        f"provider_event_operation:{fixture.operation_id}"
    )
    assert {result.status for result in results} == {
        DownstreamAdoptionStatus.adopted.value,
    }

    verify = factory()
    try:
        refreshed = (
            verify.query(ProviderEventDispatch)
            .filter_by(operation_id=fixture.operation_id)
            .one()
        )
        assert refreshed.downstream_adoption_fencing_token == owners[0].fencing_token
        assert (
            refreshed.downstream_adoption_status
            == DownstreamAdoptionStatus.adopted.value
        )
    finally:
        verify.close()


def test_same_claimant_reclaim_renews_lease_without_advancing_fence(
    dbsession: Session,
) -> None:
    fixture = _seed_dispatch(dbsession)
    service = ProviderEventDispatchAdoptionService(dbsession)
    launch_identity = f"job-{fixture.operation_id}"
    first = service.claim(
        authorization=_authorization(fixture),
        claimant_id="rail-a",
        launch_identity=launch_identity,
        lease_ttl=timedelta(seconds=30),
    )
    dbsession.commit()
    assert first.owns_launch is True
    first_token = first.fencing_token
    first_expiry = (
        dbsession.query(ProviderEventDispatch)
        .filter_by(operation_id=fixture.operation_id)
        .one()
        .downstream_adoption_lease_expires_at
    )

    second = service.claim(
        authorization=_authorization(fixture),
        claimant_id="rail-a",
        launch_identity=launch_identity,
        lease_ttl=timedelta(seconds=120),
    )
    dbsession.commit()
    assert second.owns_launch is True
    assert second.fencing_token == first_token
    assert second.launch_identity == launch_identity

    refreshed = (
        dbsession.query(ProviderEventDispatch)
        .filter_by(operation_id=fixture.operation_id)
        .one()
    )
    assert refreshed.downstream_adoption_fencing_token == first_token
    assert refreshed.downstream_adoption_lease_owner == "rail-a"
    assert refreshed.downstream_adoption_ref == launch_identity
    assert refreshed.downstream_adoption_lease_expires_at > first_expiry


def test_reclaim_after_launch_before_report_adopts_same_sink_and_fences_stale_claimant(
    dbsession: Session,
) -> None:
    """Crash after recording sink identity but before report_started."""

    fixture = _seed_dispatch(dbsession)
    service = ProviderEventDispatchAdoptionService(dbsession)
    launch_identity = f"job-{fixture.operation_id}"
    first = service.claim(
        authorization=_authorization(fixture),
        claimant_id="rail-a",
        launch_identity=launch_identity,
    )
    dbsession.commit()
    assert first.owns_launch is True
    stale_token = first.fencing_token

    dispatch = (
        dbsession.query(ProviderEventDispatch)
        .filter_by(operation_id=fixture.operation_id)
        .one()
    )
    assert dispatch.downstream_adoption_ref == launch_identity
    # Lease expires after launch I/O but before started acknowledgement.
    dispatch.downstream_adoption_lease_expires_at = datetime.now(timezone.utc) - (
        timedelta(seconds=1)
    )
    dbsession.commit()

    second = service.claim(
        authorization=_authorization(fixture),
        claimant_id="rail-b",
        launch_identity=launch_identity,
    )
    dbsession.commit()
    assert second.owns_launch is True
    assert second.fencing_token == stale_token + 1
    assert second.launch_identity == launch_identity

    with pytest.raises(DispatchAdoptionError) as started_exc:
        service.report_started(
            operation_id=fixture.operation_id,
            fencing_token=stale_token,
            launch_identity=launch_identity,
        )
    assert started_exc.value.reason_code == "fencing_token_mismatch"
    dbsession.rollback()

    with pytest.raises(DispatchAdoptionError) as terminal_exc:
        service.report_terminal(
            operation_id=fixture.operation_id,
            fencing_token=stale_token,
            terminal_reason="stale_owner",
        )
    assert terminal_exc.value.reason_code == "fencing_token_mismatch"
    dbsession.rollback()

    started = service.report_started(
        operation_id=fixture.operation_id,
        fencing_token=second.fencing_token,
        launch_identity=launch_identity,
    )
    dbsession.commit()
    assert started.status == DownstreamAdoptionStatus.started.value
    assert started.launch_identity == launch_identity

    final = (
        dbsession.query(ProviderEventDispatch)
        .filter_by(operation_id=fixture.operation_id)
        .one()
    )
    assert final.downstream_adoption_status == DownstreamAdoptionStatus.started.value
    assert final.downstream_adoption_ref == launch_identity
    assert final.downstream_adoption_fencing_token == second.fencing_token


def test_started_and_terminal_adoption_are_durable_and_idempotent(
    dbsession: Session,
) -> None:
    fixture = _seed_dispatch(dbsession)
    service = ProviderEventDispatchAdoptionService(dbsession)
    claimed = service.claim(
        authorization=_authorization(fixture),
        claimant_id="rail-a",
        launch_identity=f"job-{fixture.operation_id}",
    )
    dbsession.commit()

    first_started = service.report_started(
        operation_id=fixture.operation_id,
        fencing_token=claimed.fencing_token,
        launch_identity=f"job-{fixture.operation_id}",
    )
    dbsession.commit()
    second_started = service.report_started(
        operation_id=fixture.operation_id,
        fencing_token=claimed.fencing_token,
        launch_identity=f"job-{fixture.operation_id}",
    )
    dbsession.commit()
    assert first_started.status == DownstreamAdoptionStatus.started.value
    assert second_started.status == DownstreamAdoptionStatus.started.value

    replay = service.claim(
        authorization=_authorization(fixture),
        claimant_id="rail-b",
        launch_identity=f"job-{fixture.operation_id}",
    )
    dbsession.commit()
    assert replay.owns_launch is False
    assert replay.status == DownstreamAdoptionStatus.started.value

    offline = _seed_dispatch(
        dbsession,
        dispatch_mode="offline",
        audience=COMMUNICATION_DISPATCH_AUDIENCE,
    )
    offline_service = ProviderEventDispatchAdoptionService(dbsession)
    offline_claim = offline_service.claim(
        authorization=_authorization(offline),
        claimant_id="rail-offline",
        launch_identity=f"job-{offline.operation_id}",
    )
    dbsession.commit()
    first_terminal = offline_service.report_terminal(
        operation_id=offline.operation_id,
        fencing_token=offline_claim.fencing_token,
        terminal_reason="offline_job_launch_failed",
    )
    dbsession.commit()
    second_terminal = offline_service.report_terminal(
        operation_id=offline.operation_id,
        fencing_token=offline_claim.fencing_token,
        terminal_reason="offline_job_launch_failed",
    )
    dbsession.commit()
    assert first_terminal.status == DownstreamAdoptionStatus.terminal.value
    assert second_terminal.status == DownstreamAdoptionStatus.terminal.value


@pytest.mark.parametrize(
    "field_name,mutator",
    [
        (
            "run_id",
            lambda auth: DispatchAuthorizationSnapshot(
                **{**auth.__dict__, "run_id": auth.run_id + 1},
            ),
        ),
        (
            "run_key",
            lambda auth: DispatchAuthorizationSnapshot(
                **{**auth.__dict__, "run_key": auth.run_key + "-mutated"},
            ),
        ),
        (
            "assistant_id",
            lambda auth: DispatchAuthorizationSnapshot(
                **{**auth.__dict__, "assistant_id": auth.assistant_id + 1},
            ),
        ),
        (
            "task_id",
            lambda auth: DispatchAuthorizationSnapshot(
                **{**auth.__dict__, "task_id": auth.task_id + 1},
            ),
        ),
        (
            "binding_id",
            lambda auth: DispatchAuthorizationSnapshot(
                **{**auth.__dict__, "binding_id": auth.binding_id + "-x"},
            ),
        ),
        (
            "receipt_id",
            lambda auth: DispatchAuthorizationSnapshot(
                **{**auth.__dict__, "receipt_id": auth.receipt_id + "-x"},
            ),
        ),
        (
            "accepted_revision",
            lambda auth: DispatchAuthorizationSnapshot(
                **{
                    **auth.__dict__,
                    "accepted_revision": auth.accepted_revision + "-x",
                },
            ),
        ),
        (
            "dispatch_mode",
            lambda auth: DispatchAuthorizationSnapshot(
                **{**auth.__dict__, "dispatch_mode": "offline"},
            ),
        ),
        (
            "audience",
            lambda auth: DispatchAuthorizationSnapshot(
                **{**auth.__dict__, "audience": COMMUNICATION_DISPATCH_AUDIENCE},
            ),
        ),
    ],
)
def test_reused_operation_rejects_changed_authorization_snapshot(
    dbsession: Session,
    field_name: str,
    mutator,
) -> None:
    fixture = _seed_dispatch(dbsession)
    service = ProviderEventDispatchAdoptionService(dbsession)
    original = _authorization(fixture)
    service.claim(
        authorization=original,
        claimant_id="rail-a",
        launch_identity=f"job-{fixture.operation_id}",
    )
    dbsession.commit()

    with pytest.raises(DispatchAdoptionError) as exc:
        service.claim(
            authorization=mutator(original),
            claimant_id="rail-b",
            launch_identity=f"job-{fixture.operation_id}",
        )
    assert exc.value.reason_code == "dispatch_authorization_mismatch"
    dbsession.rollback()

    refreshed = (
        dbsession.query(ProviderEventDispatch)
        .filter_by(operation_id=fixture.operation_id)
        .one()
    )
    assert refreshed.run_key == fixture.run_key
    assert refreshed.downstream_adoption_lease_owner == "rail-a"
    assert field_name


@pytest.mark.anyio
async def test_adoption_routes_enforce_assistant_ownership(
    dbsession: Session,
    client: AsyncClient,
) -> None:
    fixture = _seed_dispatch(dbsession)
    dbsession.commit()

    owner_response = await client.post(
        USER_CLAIM_PATH,
        json=_claim_payload(fixture, claimant_id="unity-owner"),
        headers=HEADERS,
    )
    assert owner_response.status_code == 200, owner_response.text
    assert owner_response.json()["owns_launch"] is True

    other_response = await client.post(
        USER_CLAIM_PATH,
        json=_claim_payload(fixture, claimant_id="unity-other"),
        headers=HEADERS_2,
    )
    assert other_response.status_code in {403, 404}, other_response.text

    admin_response = await client.post(
        CLAIM_PATH,
        json=_claim_payload(fixture, claimant_id="communication-admin"),
        headers=ADMIN_HEADERS,
    )
    assert admin_response.status_code == 200, admin_response.text
    body = admin_response.json()
    assert body["status"] == DownstreamAdoptionStatus.adopted.value
    assert body["owns_launch"] is False


@pytest.mark.anyio
async def test_worker_converges_from_orchestra_adoption_and_run_state_without_rail_get(
    dbsession: Session,
) -> None:
    fixture = _seed_dispatch(
        dbsession,
        dispatch_mode="offline",
        audience=COMMUNICATION_DISPATCH_AUDIENCE,
    )
    service = ProviderEventDispatchAdoptionService(dbsession)
    claimed = service.claim(
        authorization=_authorization(fixture),
        claimant_id="communication-1",
        launch_identity=f"unity-task-execution-{fixture.operation_id}",
    )
    service.report_started(
        operation_id=fixture.operation_id,
        fencing_token=claimed.fencing_token,
        launch_identity=f"unity-task-execution-{fixture.operation_id}",
    )
    dispatch = (
        dbsession.query(ProviderEventDispatch)
        .filter_by(operation_id=fixture.operation_id)
        .one()
    )
    dispatch.processing_state = DispatchProcessingState.delivered.value
    dispatch.next_retry_at = datetime.now(timezone.utc)
    dbsession.commit()

    delivery = ProviderEventDispatchDeliveryService(dbsession)
    stats = delivery.process_status_convergence_batch(batch_size=10)
    dbsession.commit()
    assert stats["dispatches_converged"] >= 1

    refreshed = (
        dbsession.query(ProviderEventDispatch)
        .filter_by(operation_id=fixture.operation_id)
        .one()
    )
    assert refreshed.processing_state == DispatchProcessingState.started.value

    update_task_run(
        dbsession,
        fixture.project_id,
        assistant_id=str(fixture.assistant_id),
        run_key=fixture.run_key,
        updates={"state": "completed", "result_summary": "done"},
    )
    refreshed.next_retry_at = datetime.now(timezone.utc)
    dbsession.commit()
    terminal_stats = delivery.process_status_convergence_batch(batch_size=10)
    dbsession.commit()
    assert terminal_stats["dispatches_terminal_succeeded"] >= 1
    final = (
        dbsession.query(ProviderEventDispatch)
        .filter_by(operation_id=fixture.operation_id)
        .one()
    )
    assert final.processing_state == DispatchProcessingState.succeeded.value
    assert final.downstream_adoption_status == DownstreamAdoptionStatus.terminal.value


@pytest.mark.anyio
async def test_worker_converges_a_held_run_as_terminal_failed(
    dbsession: Session,
) -> None:
    """A held run is terminal and the effect did not happen.

    The assistant's runtime holds a run when a verification it depended on
    failed or could not be settled; nothing was sent or changed and the owner
    was told why. Convergence must not leave the dispatch in flight forever,
    and must not call it succeeded: it failed with the run's own reason.
    """
    fixture = _seed_dispatch(
        dbsession,
        dispatch_mode="offline",
        audience=COMMUNICATION_DISPATCH_AUDIENCE,
    )
    service = ProviderEventDispatchAdoptionService(dbsession)
    claimed = service.claim(
        authorization=_authorization(fixture),
        claimant_id="communication-1",
        launch_identity=f"unity-task-execution-{fixture.operation_id}",
    )
    service.report_started(
        operation_id=fixture.operation_id,
        fencing_token=claimed.fencing_token,
        launch_identity=f"unity-task-execution-{fixture.operation_id}",
    )
    dispatch = (
        dbsession.query(ProviderEventDispatch)
        .filter_by(operation_id=fixture.operation_id)
        .one()
    )
    dispatch.processing_state = DispatchProcessingState.started.value
    dispatch.next_retry_at = datetime.now(timezone.utc)
    dbsession.commit()

    update_task_run(
        dbsession,
        fixture.project_id,
        assistant_id=str(fixture.assistant_id),
        run_key=fixture.run_key,
        updates={
            "state": "held",
            "held_reason": "unsettled_verdict: send_digest could not be verified",
        },
    )
    dbsession.commit()

    delivery = ProviderEventDispatchDeliveryService(dbsession)
    stats = delivery.process_status_convergence_batch(batch_size=10)
    dbsession.commit()
    assert stats["dispatches_terminal_failed"] >= 1

    final = (
        dbsession.query(ProviderEventDispatch)
        .filter_by(operation_id=fixture.operation_id)
        .one()
    )
    assert final.processing_state == DispatchProcessingState.failed.value
    assert final.downstream_adoption_status == DownstreamAdoptionStatus.terminal.value
    assert final.downstream_adoption_ref == "held"
    assert final.terminal_error_code == (
        "unsettled_verdict: send_digest could not be verified"
    )


@pytest.mark.anyio
async def test_admin_claim_and_report_http_round_trip(
    dbsession: Session,
    client: AsyncClient,
) -> None:
    fixture = _seed_dispatch(
        dbsession,
        dispatch_mode="offline",
        audience=COMMUNICATION_DISPATCH_AUDIENCE,
    )
    dbsession.commit()

    claim = await client.post(
        CLAIM_PATH,
        json=_claim_payload(fixture, claimant_id="communication-http"),
        headers=ADMIN_HEADERS,
    )
    assert claim.status_code == 200, claim.text
    claim_body = claim.json()
    assert claim_body["owns_launch"] is True
    token = claim_body["fencing_token"]

    started = await client.post(
        REPORT_STARTED_PATH,
        json={
            "operation_id": fixture.operation_id,
            "fencing_token": token,
            "launch_identity": claim_body["launch_identity"],
        },
        headers=ADMIN_HEADERS,
    )
    assert started.status_code == 200, started.text
    assert started.json()["status"] == DownstreamAdoptionStatus.started.value

    stale = await client.post(
        REPORT_TERMINAL_PATH,
        json={
            "operation_id": fixture.operation_id,
            "fencing_token": token - 1 if token else 0,
            "terminal_reason": "should_fail",
        },
        headers=ADMIN_HEADERS,
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["reason"] == "fencing_token_mismatch"
