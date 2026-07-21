"""Integration tests for provider-event context lifecycle."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dao.provider_trigger_dao import ProviderTriggerDAO
from orchestra.db.models.core_models import Project
from orchestra.db.models.orchestra_models import Assistant
from orchestra.db.models.provider_trigger_models import (
    ProviderEventBlobAudit,
    ProviderEventBlobDeletion,
    ProviderEventReceipt,
)
from orchestra.provider_triggers.dispatch_request import EVENT_CONTEXT_AUDIENCE
from orchestra.provider_triggers.event_context_retention import (
    resolve_event_context_expires_at,
)
from orchestra.provider_triggers.runtime_types import (
    BlobAuditAction,
    EventContextUnavailableReason,
)
from orchestra.provider_triggers.task_trigger import ProviderEventTrigger
from orchestra.services.provider_event_blob_service import ProviderEventBlobService
from orchestra.services.task_machine_state_service import (
    TASK_MACHINE_PROJECT_NAME,
    create_task_run_if_absent,
)
from orchestra.tests.test_log import HEADERS
from orchestra.workers.provider_trigger_worker import run_worker_cycle

PRIMARY_USER_ID = str(os.getenv("AUTH_ACCOUNT_USER_ID"))
EVENT_CONTEXT_PATH = "/v0/provider-event/event-context"
SOURCE_BODY = {"issue": "opened", "number": 7}
ENVELOPE = {
    "provider_trigger_slug": "GITHUB_ISSUE_CREATED_TRIGGER",
    "delivery_id": "gh-1",
}
CURATED_PROJECTION = {"title": "Something broke", "author": "octocat"}


class _EventContextFixture:
    """Bundle of the durable rows one dispatched provider-event run needs."""

    def __init__(
        self,
        *,
        assistant_id: int,
        task_id: int,
        run_id: int,
        receipt_id: str,
        event_context_ref: str,
        expires_at: datetime,
    ) -> None:
        self.assistant_id = assistant_id
        self.task_id = task_id
        self.run_id = run_id
        self.receipt_id = receipt_id
        self.event_context_ref = event_context_ref
        self.expires_at = expires_at


def _fresh_issued_at() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seed_event_context(dbsession: Session) -> _EventContextFixture:
    """Create a real owned assistant, run, binding, receipt, and attached blob."""

    assistant = Assistant(user_id=PRIMARY_USER_ID, first_name="Trigger", surname="Bot")
    dbsession.add(assistant)
    dbsession.flush()

    project = Project(name=TASK_MACHINE_PROJECT_NAME, user_id=PRIMARY_USER_ID)
    dbsession.add(project)
    dbsession.flush()

    task_id = 4242
    receipt_id = f"receipt-{assistant.agent_id}-{task_id}"
    run, _ = create_task_run_if_absent(
        dbsession,
        project.id,
        {
            "run_key": f"run-{assistant.agent_id}-{task_id}-1",
            "assistant_id": str(assistant.agent_id),
            "task_id": task_id,
            "wake": "provider_event",
            "delivery": "live",
            "state": "pending",
            "provider_event_receipt_id": receipt_id,
        },
    )

    dao = ProviderTriggerDAO(dbsession)
    trigger = ProviderEventTrigger(
        state="enabled",
        connection_id="conn-test",
        backend_id="composio",
        canonical_app_slug="github",
        provider_trigger_slug="GITHUB_ISSUE_CREATED_TRIGGER",
        trigger_config={},
    )
    binding = dao.create_binding(
        binding_id=f"binding-{assistant.agent_id}-{task_id}",
        project_id=project.id,
        tasks_context_id=0,
        source_task_log_id=run.id,
        task_id=task_id,
        assistant_id=assistant.agent_id,
        task_revision=1,
        trigger=trigger,
        execution_mode="live",
        entrypoint=None,
    )
    generation = dao.create_generation(binding=binding)
    expires_at = resolve_event_context_expires_at()
    receipt = dao.adopt_receipt(
        binding=binding,
        generation=generation,
        provider_event_identity_hmac="event-identity-endpoint",
        receipt_id=receipt_id,
        event_context_expires_at=expires_at,
    )
    receipt.run_id = run.id
    receipt.stable_envelope_json = ENVELOPE
    receipt.curated_projection_json = CURATED_PROJECTION

    blob_service = ProviderEventBlobService(dbsession)
    blob = blob_service.write_uncommitted(
        binding_id=binding.binding_id,
        receipt_id=receipt.receipt_id,
        plaintext=b'{"issue":"opened","number":7}',
    )
    blob_service.attach_event_context(receipt=receipt, blob=blob)
    dbsession.flush()

    return _EventContextFixture(
        assistant_id=assistant.agent_id,
        task_id=task_id,
        run_id=run.id,
        receipt_id=receipt.receipt_id,
        event_context_ref=receipt.event_context_ref,
        expires_at=expires_at,
    )


def _service_payload(fixture: _EventContextFixture, **overrides) -> dict:
    payload = {
        "assistant_id": str(fixture.assistant_id),
        "task_id": fixture.task_id,
        "run_id": fixture.run_id,
        "receipt_id": fixture.receipt_id,
        "event_context_ref": fixture.event_context_ref,
        "audience": EVENT_CONTEXT_AUDIENCE,
        "issued_at": _fresh_issued_at(),
    }
    payload.update(overrides)
    return payload


@pytest.mark.anyio
async def test_service_event_context_requires_fresh_issued_at_and_exact_run_receipt_linkage(
    dbsession: Session,
    client: AsyncClient,
) -> None:
    fixture = _seed_event_context(dbsession)

    response = await client.post(
        EVENT_CONTEXT_PATH,
        json=_service_payload(fixture),
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["receipt_id"] == fixture.receipt_id
    assert body["run_id"] == fixture.run_id
    assert body["event_context_ref"] == fixture.event_context_ref
    assert body["envelope"] == ENVELOPE
    assert body["curated_projection"] == CURATED_PROJECTION
    assert body["source_body"] == SOURCE_BODY
    assert body["expires_at"] is not None

    stale = datetime.now(timezone.utc) - timedelta(hours=1)
    stale_response = await client.post(
        EVENT_CONTEXT_PATH,
        json=_service_payload(fixture, issued_at=stale.isoformat()),
        headers=HEADERS,
    )
    assert stale_response.status_code == 404
    assert stale_response.json()["detail"] == "event_context_token_expired"

    future = datetime.now(timezone.utc) + timedelta(hours=1)
    future_response = await client.post(
        EVENT_CONTEXT_PATH,
        json=_service_payload(fixture, issued_at=future.isoformat()),
        headers=HEADERS,
    )
    assert future_response.status_code == 404
    assert future_response.json()["detail"] == "event_context_token_expired"

    mismatch_response = await client.post(
        EVENT_CONTEXT_PATH,
        json=_service_payload(fixture, task_id=fixture.task_id + 1),
        headers=HEADERS,
    )
    assert mismatch_response.status_code == 404
    assert mismatch_response.json()["detail"] == "event_context_unavailable"


@pytest.mark.anyio
async def test_event_context_rejects_wrong_audience(
    dbsession: Session,
    client: AsyncClient,
) -> None:
    fixture = _seed_event_context(dbsession)

    response = await client.post(
        EVENT_CONTEXT_PATH,
        json=_service_payload(
            fixture,
            audience="unity:provider-event-dispatch",
        ),
        headers=HEADERS,
    )

    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "invalid_event_context_audience"


@pytest.mark.anyio
async def test_owned_event_context_routes_read_export_delete_and_remain_deleted(
    dbsession: Session,
    client: AsyncClient,
) -> None:
    fixture = _seed_event_context(dbsession)
    base = (
        f"/v0/assistants/{fixture.assistant_id}/tasks/{fixture.task_id}"
        f"/runs/{fixture.run_id}/event-context"
    )

    read_response = await client.get(base, headers=HEADERS)
    assert read_response.status_code == 200, read_response.text
    read_body = read_response.json()["info"]
    assert read_body["source_body"] == SOURCE_BODY
    assert read_body["expires_at"] is not None

    export_response = await client.post(f"{base}/export", headers=HEADERS)
    assert export_response.status_code == 200, export_response.text
    assert export_response.json()["info"]["source_body"] == SOURCE_BODY

    export_audits = dbsession.execute(
        select(ProviderEventBlobAudit).where(
            ProviderEventBlobAudit.action == BlobAuditAction.export.value,
            ProviderEventBlobAudit.receipt_id == fixture.receipt_id,
        ),
    ).scalars()
    assert list(export_audits)

    delete_response = await client.delete(base, headers=HEADERS)
    assert delete_response.status_code == 204, delete_response.text

    receipt = dbsession.execute(
        select(ProviderEventReceipt).where(
            ProviderEventReceipt.receipt_id == fixture.receipt_id,
        ),
    ).scalar_one()
    assert (
        receipt.event_context_unavailable_reason
        == EventContextUnavailableReason.deleted.value
    )
    assert receipt.event_context_ref is None
    assert receipt.stable_envelope_json is None
    assert receipt.curated_projection_json is None

    deletion_rows = list(
        dbsession.execute(select(ProviderEventBlobDeletion)).scalars(),
    )
    assert deletion_rows

    deleted_read = await client.get(base, headers=HEADERS)
    assert deleted_read.status_code == 404
    assert deleted_read.json()["detail"] == "event_context_deleted"

    repeat_delete = await client.delete(base, headers=HEADERS)
    assert repeat_delete.status_code == 204


def test_worker_expires_context_then_deletes_ciphertext(
    dbsession: Session,
) -> None:
    fixture = _seed_event_context(dbsession)
    receipt = dbsession.execute(
        select(ProviderEventReceipt).where(
            ProviderEventReceipt.receipt_id == fixture.receipt_id,
        ),
    ).scalar_one()
    receipt.event_context_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    dbsession.commit()

    first_cycle = run_worker_cycle()
    assert first_cycle["event_contexts_expired"] == 1

    dbsession.expire_all()
    receipt = dbsession.execute(
        select(ProviderEventReceipt).where(
            ProviderEventReceipt.receipt_id == fixture.receipt_id,
        ),
    ).scalar_one()
    assert (
        receipt.event_context_unavailable_reason
        == EventContextUnavailableReason.expired.value
    )
    assert receipt.event_context_ref is None
    assert receipt.stable_envelope_json is None

    second_cycle = run_worker_cycle()
    assert second_cycle["blob_deletions_processed"] >= 1
