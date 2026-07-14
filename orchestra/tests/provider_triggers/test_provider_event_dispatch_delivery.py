"""Production-path tests for provider-event dispatch delivery and convergence."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dao.provider_trigger_dao import ProviderTriggerDAO
from orchestra.db.models.core_models import Project
from orchestra.db.models.orchestra_models import Assistant
from orchestra.db.models.provider_trigger_models import (
    ProviderEventDispatch,
    ProviderTriggerWorkerHeartbeat,
)
from orchestra.provider_triggers.dispatch_request import COMMUNICATION_DISPATCH_AUDIENCE
from orchestra.provider_triggers.runtime_types import (
    DispatchErrorCode,
    DispatchProcessingState,
    ReceiptProcessingState,
)
from orchestra.provider_triggers.task_trigger import ProviderEventTrigger
from orchestra.services.provider_event_blob_service import ProviderEventBlobService
from orchestra.services.provider_event_dispatch_delivery_service import (
    ProviderEventDispatchDeliveryService,
)
from orchestra.services.task_machine_state_service import (
    TASK_MACHINE_PROJECT_NAME,
    create_task_run_if_absent,
    get_task_run,
    update_task_run,
)
from orchestra.workers.provider_trigger_worker import WORKER_KEY, run_worker_cycle

PRIMARY_USER_ID = str(os.getenv("AUTH_ACCOUNT_USER_ID"))
ADMIN_HEADERS = {"Authorization": "Bearer test-admin-key"}


@dataclass
class _DispatchFixture:
    operation_id: str
    binding_id: str
    receipt_id: str
    run_id: int
    run_key: str
    assistant_id: int
    task_id: int
    project_id: int
    dispatch_mode: str


@dataclass
class _FakeHttpResponse:
    status_code: int
    payload: dict[str, Any] = field(default_factory=dict)

    def json(self) -> dict[str, Any]:
        return self.payload


class _RecordingHttpClient:
    """Boundary double for outbound Comm/Adapters HTTP calls."""

    def __init__(
        self,
        *,
        post_handler: Any | None = None,
        get_handler: Any | None = None,
    ) -> None:
        self.post_handler = post_handler
        self.get_handler = get_handler
        self.post_calls: list[tuple[str, dict[str, Any]]] = []
        self.get_calls: list[tuple[str, dict[str, Any]]] = []

    def post(self, url: str, **kwargs: Any) -> _FakeHttpResponse:
        self.post_calls.append((url, kwargs))
        if self.post_handler is None:
            return _FakeHttpResponse(502)
        return self.post_handler(url, kwargs)

    def get(self, url: str, **kwargs: Any) -> _FakeHttpResponse:
        self.get_calls.append((url, kwargs))
        if self.get_handler is None:
            return _FakeHttpResponse(404)
        return self.get_handler(url, kwargs)

    def __enter__(self) -> _RecordingHttpClient:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def _patch_delivery_http_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    http_client: _RecordingHttpClient,
) -> None:
    original_init = ProviderEventDispatchDeliveryService.__init__

    def patched_init(
        self,
        session,
        *,
        lease_owner=None,
        http_client_factory=None,
    ) -> None:
        original_init(
            self,
            session,
            lease_owner=lease_owner,
            http_client_factory=lambda: http_client,
        )

    monkeypatch.setattr(ProviderEventDispatchDeliveryService, "__init__", patched_init)


def _seed_pending_dispatch(
    dbsession: Session,
    *,
    dispatch_mode: str = "offline",
    operation_id: str | None = None,
) -> _DispatchFixture:
    assistant = Assistant(user_id=PRIMARY_USER_ID, first_name="Dispatch", surname="Bot")
    dbsession.add(assistant)
    dbsession.flush()

    project = Project(name=TASK_MACHINE_PROJECT_NAME, user_id=PRIMARY_USER_ID)
    dbsession.add(project)
    dbsession.flush()

    task_id = 5150
    run_key = f"run-{assistant.agent_id}-{task_id}-dispatch"
    run, _ = create_task_run_if_absent(
        dbsession,
        project.id,
        {
            "run_key": run_key,
            "assistant_id": str(assistant.agent_id),
            "task_id": task_id,
            "source_type": "provider_event",
            "execution_mode": dispatch_mode,
            "state": "pending",
        },
    )

    dao = ProviderTriggerDAO(dbsession)
    binding_id = f"binding-{uuid.uuid4().hex[:8]}"
    trigger = ProviderEventTrigger(
        state="enabled",
        connection_id="conn-dispatch-test",
        backend_id="composio",
        canonical_app_slug="github",
        event_slug="github.issue_created",
        schema_version="1",
    )
    binding = dao.create_binding(
        binding_id=binding_id,
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
        provider_event_identity_hmac="dispatch-identity-hmac",
        receipt_id=f"receipt-{uuid.uuid4().hex[:8]}",
    )
    blob_service = ProviderEventBlobService(dbsession)
    blob = blob_service.write_uncommitted(
        binding_id=binding.binding_id,
        receipt_id=receipt.receipt_id,
        plaintext=b'{"issue":"opened"}',
    )
    blob_service.attach_event_context(receipt=receipt, blob=blob)

    audience = (
        COMMUNICATION_DISPATCH_AUDIENCE
        if dispatch_mode == "offline"
        else "unity:provider-event-dispatch"
    )
    dispatch = dao.adopt_dispatch(
        receipt=receipt,
        binding=binding,
        run_id=run.id,
        run_key=run_key,
        audience=audience,
        operation_id=operation_id,
    )
    dbsession.flush()
    return _DispatchFixture(
        operation_id=dispatch.operation_id,
        binding_id=binding.binding_id,
        receipt_id=receipt.receipt_id,
        run_id=run.id,
        run_key=run_key,
        assistant_id=assistant.agent_id,
        task_id=task_id,
        project_id=project.id,
        dispatch_mode=dispatch_mode,
    )


def _delivery_service(
    dbsession: Session,
    *,
    http_client: _RecordingHttpClient,
    lease_owner: str = "dispatch-test-worker",
) -> ProviderEventDispatchDeliveryService:
    return ProviderEventDispatchDeliveryService(
        dbsession,
        lease_owner=lease_owner,
        http_client_factory=lambda: http_client,
    )


def test_claim_dispatches_for_delivery_reclaims_expired_lease(
    dbsession: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _seed_pending_dispatch(dbsession)
    dispatch = dbsession.execute(
        select(ProviderEventDispatch).where(
            ProviderEventDispatch.operation_id == fixture.operation_id,
        ),
    ).scalar_one()
    dispatch.processing_state = DispatchProcessingState.claimed.value
    dispatch.lease_owner = "stale-owner"
    dispatch.lease_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    dbsession.flush()

    post_calls: list[tuple[str, dict[str, Any]]] = []

    def post_handler(url: str, kwargs: dict[str, Any]) -> _FakeHttpResponse:
        post_calls.append((url, kwargs))
        return _FakeHttpResponse(
            200,
            {
                "status": "adopted",
                "adopted_only": False,
                "job_name": "unity-task-run-test",
            },
        )

    monkeypatch.setenv("UNITY_COMMS_URL", "http://comms.test")
    monkeypatch.setenv("ORCHESTRA_ADMIN_KEY", "test-admin-key")
    service = _delivery_service(
        dbsession,
        http_client=_RecordingHttpClient(post_handler=post_handler),
        lease_owner="fresh-owner",
    )

    stats = service.process_dispatch_batch()
    dbsession.commit()

    refreshed = dbsession.execute(
        select(ProviderEventDispatch).where(
            ProviderEventDispatch.operation_id == fixture.operation_id,
        ),
    ).scalar_one()
    assert stats["dispatches_claimed"] == 1
    assert stats["dispatches_delivered"] == 1
    assert refreshed.processing_state == DispatchProcessingState.delivered.value
    assert refreshed.lease_owner is None
    assert len(post_calls) == 1
    assert post_calls[0][1]["json"]["operation_id"] == fixture.operation_id


def test_offline_delivery_retry_converges_without_second_launch_identity(
    dbsession: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _seed_pending_dispatch(dbsession)
    call_count = {"posts": 0}

    def post_handler(url: str, _kwargs: dict[str, Any]) -> _FakeHttpResponse:
        call_count["posts"] += 1
        if call_count["posts"] == 1:
            return _FakeHttpResponse(
                200,
                {
                    "status": "started",
                    "adopted_only": False,
                    "job_name": "unity-task-run-1",
                },
            )
        return _FakeHttpResponse(
            200,
            {
                "status": "started",
                "adopted_only": True,
                "job_name": "unity-task-run-1",
            },
        )

    monkeypatch.setenv("UNITY_COMMS_URL", "http://comms.test")
    monkeypatch.setenv("ORCHESTRA_ADMIN_KEY", "test-admin-key")
    service = _delivery_service(
        dbsession,
        http_client=_RecordingHttpClient(post_handler=post_handler),
    )

    first_stats = service.process_dispatch_batch()
    dbsession.commit()
    dispatch = dbsession.execute(
        select(ProviderEventDispatch).where(
            ProviderEventDispatch.operation_id == fixture.operation_id,
        ),
    ).scalar_one()
    dispatch.processing_state = DispatchProcessingState.claimed.value
    dispatch.lease_owner = "stale-owner"
    dispatch.lease_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    dispatch.next_retry_at = datetime.now(timezone.utc)
    dbsession.flush()

    second_stats = service.process_dispatch_batch()
    dbsession.commit()

    refreshed = dbsession.execute(
        select(ProviderEventDispatch).where(
            ProviderEventDispatch.operation_id == fixture.operation_id,
        ),
    ).scalar_one()
    assert first_stats["dispatches_started"] == 1
    assert second_stats["dispatches_duplicate_prevented"] == 1
    assert refreshed.processing_state == DispatchProcessingState.started.value
    assert call_count["posts"] == 2
    assert refreshed.operation_id == fixture.operation_id


def test_offline_terminal_rejection_records_stable_reason_without_new_run(
    dbsession: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _seed_pending_dispatch(dbsession)

    def post_handler(_url: str, _kwargs: dict[str, Any]) -> _FakeHttpResponse:
        return _FakeHttpResponse(409, {"reason": "dispatch_inbox_mismatch"})

    monkeypatch.setenv("UNITY_COMMS_URL", "http://comms.test")
    monkeypatch.setenv("ORCHESTRA_ADMIN_KEY", "test-admin-key")
    service = _delivery_service(
        dbsession,
        http_client=_RecordingHttpClient(post_handler=post_handler),
    )

    stats = service.process_dispatch_batch()
    dbsession.commit()

    dispatch = dbsession.execute(
        select(ProviderEventDispatch).where(
            ProviderEventDispatch.operation_id == fixture.operation_id,
        ),
    ).scalar_one()
    run_row = get_task_run(
        dbsession,
        fixture.project_id,
        fixture.run_key,
        assistant_id=str(fixture.assistant_id),
    )
    assert stats["dispatches_failed"] == 1
    assert dispatch.processing_state == DispatchProcessingState.failed.value
    assert (
        dispatch.terminal_error_code == DispatchErrorCode.dispatch_inbox_mismatch.value
    )
    assert run_row is not None
    assert run_row.id == fixture.run_id


def test_live_delivery_marks_delivered_and_converges_run_state(
    dbsession: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _seed_pending_dispatch(dbsession, dispatch_mode="live")

    def post_handler(url: str, _kwargs: dict[str, Any]) -> _FakeHttpResponse:
        assert "/unity/system-event" in url
        return _FakeHttpResponse(200, {"ok": True})

    monkeypatch.setenv("UNITY_ADAPTERS_URL", "http://adapters.test")
    monkeypatch.setenv("ORCHESTRA_ADMIN_KEY", "test-admin-key")
    service = _delivery_service(
        dbsession,
        http_client=_RecordingHttpClient(post_handler=post_handler),
    )

    delivery_stats = service.process_dispatch_batch()
    dbsession.commit()

    dispatch = dbsession.execute(
        select(ProviderEventDispatch).where(
            ProviderEventDispatch.operation_id == fixture.operation_id,
        ),
    ).scalar_one()
    dispatch.next_retry_at = datetime.now(timezone.utc)
    dbsession.flush()

    update_task_run(
        dbsession,
        fixture.project_id,
        str(fixture.assistant_id),
        fixture.run_key,
        {"state": "running"},
        source_task_log_id=fixture.run_id,
    )
    dbsession.flush()

    converge_stats = service.process_status_convergence_batch()
    dbsession.commit()

    dispatch.next_retry_at = datetime.now(timezone.utc)
    dbsession.flush()

    update_task_run(
        dbsession,
        fixture.project_id,
        str(fixture.assistant_id),
        fixture.run_key,
        {"state": "completed"},
        source_task_log_id=fixture.run_id,
    )
    dbsession.flush()

    final_stats = service.process_status_convergence_batch()
    dbsession.commit()

    dispatch = dbsession.execute(
        select(ProviderEventDispatch).where(
            ProviderEventDispatch.operation_id == fixture.operation_id,
        ),
    ).scalar_one()
    assert delivery_stats["dispatches_delivered"] == 1
    assert converge_stats["dispatches_converged"] == 1
    assert final_stats["dispatches_terminal_succeeded"] == 1
    assert dispatch.processing_state == DispatchProcessingState.succeeded.value


def test_run_worker_cycle_records_dispatch_metrics_in_heartbeat(
    dbsession: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_pending_dispatch(dbsession)

    def post_handler(_url: str, _kwargs: dict[str, Any]) -> _FakeHttpResponse:
        return _FakeHttpResponse(
            200,
            {"status": "adopted", "adopted_only": False, "job_name": None},
        )

    monkeypatch.setenv("UNITY_COMMS_URL", "http://comms.test")
    monkeypatch.setenv("ORCHESTRA_ADMIN_KEY", "test-admin-key")
    _patch_delivery_http_client(
        monkeypatch,
        http_client=_RecordingHttpClient(post_handler=post_handler),
    )

    totals = run_worker_cycle(lease_owner="heartbeat-worker")
    dbsession.commit()

    heartbeat = dbsession.execute(
        select(ProviderTriggerWorkerHeartbeat).where(
            ProviderTriggerWorkerHeartbeat.worker_key == WORKER_KEY,
        ),
    ).scalar_one()
    metadata = heartbeat.metadata_json
    assert totals["dispatches_claimed"] >= 1
    assert metadata["dispatches_claimed"] >= 1
    assert "dispatch_backlog_oldest_age_seconds" in metadata


@pytest.mark.anyio
async def test_admin_provider_event_dispatch_routes(
    dbsession: Session,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _seed_pending_dispatch(dbsession)
    dispatch = dbsession.execute(
        select(ProviderEventDispatch).where(
            ProviderEventDispatch.operation_id == fixture.operation_id,
        ),
    ).scalar_one()
    dispatch.processing_state = DispatchProcessingState.pending.value
    dispatch.lease_owner = "stuck-owner"
    dispatch.lease_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    dbsession.commit()

    monkeypatch.setenv("ORCHESTRA_ADMIN_KEY", "test-admin-key")

    get_response = await client.get(
        f"/v0/admin/provider-event-dispatch/{fixture.operation_id}",
        headers=ADMIN_HEADERS,
    )
    assert get_response.status_code == 200, get_response.text
    assert get_response.json()["dispatch"]["operation_id"] == fixture.operation_id

    repair_response = await client.post(
        "/v0/admin/provider-event-dispatch/repair",
        json={"operation_id": fixture.operation_id},
        headers=ADMIN_HEADERS,
    )
    assert repair_response.status_code == 200, repair_response.text
    assert (
        repair_response.json()["dispatch"]["processing_state"]
        == DispatchProcessingState.pending.value
    )

    def post_handler(_url: str, _kwargs: dict[str, Any]) -> _FakeHttpResponse:
        return _FakeHttpResponse(
            200,
            {"status": "adopted", "adopted_only": False, "job_name": None},
        )

    monkeypatch.setenv("UNITY_COMMS_URL", "http://comms.test")
    _patch_delivery_http_client(
        monkeypatch,
        http_client=_RecordingHttpClient(post_handler=post_handler),
    )

    backlog_response = await client.post(
        "/v0/admin/provider-event-dispatch/process-backlog",
        json={"delivery_batch_size": 5, "convergence_batch_size": 5},
        headers=ADMIN_HEADERS,
    )
    assert backlog_response.status_code == 200, backlog_response.text
    body = backlog_response.json()
    assert body["delivery"]["dispatches_claimed"] >= 1
    assert "backlog_oldest_age_seconds" in body

    refreshed = dbsession.execute(
        select(ProviderEventDispatch).where(
            ProviderEventDispatch.operation_id == fixture.operation_id,
        ),
    ).scalar_one()
    assert refreshed.processing_state == DispatchProcessingState.delivered.value

    receipt = ProviderTriggerDAO(dbsession).get_receipt_by_id(
        receipt_id=fixture.receipt_id,
    )
    assert receipt is not None
    assert receipt.processing_state == ReceiptProcessingState.dispatched.value
