"""Integration tests for the ownership-scoped provider-event context endpoint."""

from __future__ import annotations

import os

import pytest
from httpx import AsyncClient
from sqlalchemy.orm import Session

from orchestra.db.dao.provider_trigger_dao import ProviderTriggerDAO
from orchestra.db.models.core_models import Project
from orchestra.db.models.orchestra_models import Assistant
from orchestra.provider_triggers.dispatch_request import EVENT_CONTEXT_AUDIENCE
from orchestra.provider_triggers.task_trigger import ProviderEventTrigger
from orchestra.services.provider_event_blob_service import ProviderEventBlobService
from orchestra.services.task_machine_state_service import (
    TASK_MACHINE_PROJECT_NAME,
    create_task_run_if_absent,
)
from orchestra.tests.test_log import HEADERS

PRIMARY_USER_ID = str(os.getenv("AUTH_ACCOUNT_USER_ID"))
EVENT_CONTEXT_PATH = "/v0/provider-event/event-context"
SOURCE_BODY = {"issue": "opened", "number": 7}
ENVELOPE = {"event_slug": "github.issue_created", "delivery_id": "gh-1"}
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
    ) -> None:
        self.assistant_id = assistant_id
        self.task_id = task_id
        self.run_id = run_id
        self.receipt_id = receipt_id
        self.event_context_ref = event_context_ref


def _seed_event_context(dbsession: Session) -> _EventContextFixture:
    """Create a real owned assistant, run, binding, receipt, and attached blob."""

    assistant = Assistant(user_id=PRIMARY_USER_ID, first_name="Trigger", surname="Bot")
    dbsession.add(assistant)
    dbsession.flush()

    project = Project(name=TASK_MACHINE_PROJECT_NAME, user_id=PRIMARY_USER_ID)
    dbsession.add(project)
    dbsession.flush()

    task_id = 4242
    run, _ = create_task_run_if_absent(
        dbsession,
        project.id,
        {
            "run_key": f"run-{assistant.agent_id}-{task_id}-1",
            "assistant_id": str(assistant.agent_id),
            "task_id": task_id,
            "source_type": "provider_event",
            "execution_mode": "live",
            "state": "pending",
        },
    )

    dao = ProviderTriggerDAO(dbsession)
    trigger = ProviderEventTrigger(
        state="enabled",
        connection_id="conn-test",
        backend_id="composio",
        canonical_app_slug="github",
        event_slug="github.issue_created",
        schema_version="1",
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
    receipt = dao.adopt_receipt(
        binding=binding,
        generation=generation,
        provider_event_identity_hmac="event-identity-endpoint",
        receipt_id=f"receipt-{assistant.agent_id}-{task_id}",
    )

    blob_service = ProviderEventBlobService(dbsession)
    blob = blob_service.write_uncommitted(
        binding_id=binding.binding_id,
        receipt_id=receipt.receipt_id,
        plaintext=b'{"issue":"opened","number":7}',
    )
    blob_service.attach_event_context(receipt=receipt, blob=blob)

    receipt.stable_envelope_json = ENVELOPE
    receipt.curated_projection_json = CURATED_PROJECTION
    dbsession.flush()

    return _EventContextFixture(
        assistant_id=assistant.agent_id,
        task_id=task_id,
        run_id=run.id,
        receipt_id=receipt.receipt_id,
        event_context_ref=receipt.event_context_ref,
    )


@pytest.mark.anyio
async def test_event_context_round_trips_envelope_projection_and_source(
    dbsession: Session,
    client: AsyncClient,
) -> None:
    fixture = _seed_event_context(dbsession)

    response = await client.post(
        EVENT_CONTEXT_PATH,
        json={
            "assistant_id": str(fixture.assistant_id),
            "task_id": fixture.task_id,
            "run_id": fixture.run_id,
            "receipt_id": fixture.receipt_id,
            "event_context_ref": fixture.event_context_ref,
            "audience": EVENT_CONTEXT_AUDIENCE,
        },
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


@pytest.mark.anyio
async def test_event_context_rejects_wrong_audience(
    dbsession: Session,
    client: AsyncClient,
) -> None:
    fixture = _seed_event_context(dbsession)

    response = await client.post(
        EVENT_CONTEXT_PATH,
        json={
            "assistant_id": str(fixture.assistant_id),
            "task_id": fixture.task_id,
            "run_id": fixture.run_id,
            "receipt_id": fixture.receipt_id,
            "event_context_ref": fixture.event_context_ref,
            "audience": "unity:provider-event-dispatch",
        },
        headers=HEADERS,
    )

    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "invalid_event_context_audience"


@pytest.mark.anyio
async def test_event_context_fails_closed_on_task_mismatch(
    dbsession: Session,
    client: AsyncClient,
) -> None:
    fixture = _seed_event_context(dbsession)

    response = await client.post(
        EVENT_CONTEXT_PATH,
        json={
            "assistant_id": str(fixture.assistant_id),
            "task_id": fixture.task_id + 1,
            "run_id": fixture.run_id,
            "receipt_id": fixture.receipt_id,
            "event_context_ref": fixture.event_context_ref,
            "audience": EVENT_CONTEXT_AUDIENCE,
        },
        headers=HEADERS,
    )

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "event_context_unavailable"
