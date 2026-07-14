"""Production-path tests for signed provider-trigger ingress acceptance."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dao.provider_trigger_dao import ProviderTriggerDAO
from orchestra.db.models.core_models import Project
from orchestra.db.models.integration_provider_models import IntegrationConnection
from orchestra.db.models.orchestra_models import Assistant
from orchestra.db.models.provider_trigger_models import (
    ProviderEventBlob,
    ProviderEventDispatch,
    ProviderEventReceipt,
)
from orchestra.provider_triggers.dispatch_request import UNITY_DISPATCH_AUDIENCE
from orchestra.provider_triggers.ingress_rate_limit import (
    reset_ingress_rate_limiter_for_tests,
)
from orchestra.provider_triggers.run_key import build_provider_event_run_key
from orchestra.provider_triggers.task_trigger import (
    ProviderEventTrigger,
    ProviderEventTriggerFilter,
)
from orchestra.services.task_machine_state_service import TASK_MACHINE_PROJECT_NAME
from orchestra.settings import settings
from orchestra.tests.test_log import HEADERS

FIXTURE_PATH = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "provider_trigger_contract"
    / "composio_github_issue_created.redacted.json"
)
WEBHOOK_SECRET = "composio-ingress-test-secret"
PRIMARY_USER_ID = str(os.getenv("AUTH_ACCOUNT_USER_ID"))


def _load_fixture() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _sign_body(raw_body: bytes, *, webhook_id: str) -> dict[str, str]:
    timestamp = str(int(time.time()))
    digest = base64.b64encode(
        hmac.new(
            WEBHOOK_SECRET.encode("utf-8"),
            f"{webhook_id}.{timestamp}.{raw_body.decode('utf-8')}".encode("utf-8"),
            hashlib.sha256,
        ).digest(),
    ).decode("utf-8")
    return {
        "webhook-id": webhook_id,
        "webhook-timestamp": timestamp,
        "webhook-signature": f"v1,{digest}",
    }


def _seed_active_ingress_binding(
    dbsession: Session,
    *,
    extra_filters: list[ProviderEventTriggerFilter] | None = None,
) -> tuple[str, str, int, int]:
    """Return ingress_key, binding_id, assistant_id, task_id for one live binding."""

    assistant = Assistant(
        user_id=PRIMARY_USER_ID,
        first_name="Ingress",
        surname="Bot",
    )
    dbsession.add(assistant)
    dbsession.flush()

    project = Project(name=TASK_MACHINE_PROJECT_NAME, user_id=PRIMARY_USER_ID)
    dbsession.add(project)
    dbsession.flush()

    connection_id = f"conn-{uuid.uuid4().hex[:10]}"
    dbsession.add(
        IntegrationConnection(
            connection_id=connection_id,
            owner_scope="assistant",
            assistant_id=assistant.agent_id,
            canonical_app_slug="github",
            backend_id="composio",
            provider_app_id="GITHUB",
            provider_connection_id="ca_provider_trigger_example",
            provider_user_id="assistant:provider-trigger-probe",
            status="connected",
            credential_storage="provider_vault",
        ),
    )
    dbsession.flush()

    task_id = int(uuid.uuid4().int % 1_000_000) + 1
    binding_id = f"binding-{uuid.uuid4().hex[:12]}"
    dao = ProviderTriggerDAO(dbsession)
    filters = [
        ProviderEventTriggerFilter(
            field="repository",
            operator="is",
            value="octocat/Hello-World",
        ),
        *(extra_filters or ()),
    ]
    trigger = ProviderEventTrigger(
        state="enabled",
        connection_id=connection_id,
        backend_id="composio",
        canonical_app_slug="github",
        event_slug="github.issue_created",
        schema_version="1",
        filters=filters,
    )
    binding = dao.create_binding(
        binding_id=binding_id,
        project_id=project.id,
        tasks_context_id=0,
        source_task_log_id=task_id,
        task_id=task_id,
        assistant_id=assistant.agent_id,
        task_revision=1,
        trigger=trigger,
        execution_mode="live",
        entrypoint=None,
    )
    generation = dao.create_generation(binding=binding)
    generation.external_trigger_id = "ti_provider_trigger_example"
    generation.signing_secret_ref = "env:COMPOSIO_WEBHOOK_SECRET"
    generation.signing_secret_version = "project"
    dao.promote_generation(binding=binding, generation=generation)
    dbsession.flush()
    return generation.ingress_key, binding.binding_id, assistant.agent_id, task_id


@pytest.fixture(autouse=True)
def _composio_webhook_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COMPOSIO_WEBHOOK_SECRET", WEBHOOK_SECRET)
    reset_ingress_rate_limiter_for_tests()


@pytest.mark.anyio
async def test_signed_composio_webhook_accepts_redelivery_once_and_surfaces_provider_run_provenance(
    dbsession: Session,
    client: AsyncClient,
) -> None:
    ingress_key, binding_id, assistant_id, task_id = _seed_active_ingress_binding(
        dbsession,
    )
    payload = _load_fixture()
    raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    path = f"/v0/webhooks/integrations/composio/{ingress_key}"

    first = await client.post(
        path,
        content=raw_body,
        headers=_sign_body(raw_body, webhook_id="msg_accept_1"),
    )
    second = await client.post(
        path,
        content=raw_body,
        headers=_sign_body(raw_body, webhook_id="msg_accept_2"),
    )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    first_body = first.json()
    second_body = second.json()
    assert first_body["status"] == "accepted"
    assert first_body["classification_reason"] == "matched"
    assert second_body["receipt_id"] == first_body["receipt_id"]

    receipts = (
        dbsession.execute(
            select(ProviderEventReceipt).where(
                ProviderEventReceipt.binding_id == binding_id,
            ),
        )
        .scalars()
        .all()
    )
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt.processing_state == "dispatch_pending"
    assert receipt.event_context_ref
    assert receipt.stable_envelope_json
    assert receipt.curated_projection_json

    dispatches = (
        dbsession.execute(
            select(ProviderEventDispatch).where(
                ProviderEventDispatch.binding_id == binding_id,
            ),
        )
        .scalars()
        .all()
    )
    assert len(dispatches) == 1
    dispatch = dispatches[0]
    assert dispatch.run_id == receipt.run_id
    assert dispatch.run_key == receipt.run_key
    assert dispatch.audience == UNITY_DISPATCH_AUDIENCE

    expected_run_key = build_provider_event_run_key(
        assistant_id=str(assistant_id),
        task_id=task_id,
        binding_id=binding_id,
        activation_revision=receipt.accepted_activation_revision,
        event_identity_hmac=receipt.provider_event_identity_hmac,
        execution_mode="live",
    )
    assert receipt.run_key == expected_run_key
    assert receipt.provider_event_identity_hmac == expected_run_key.split(":")[-1]

    run_response = await client.post(
        "/v0/task-run/get",
        json={
            "project_name": TASK_MACHINE_PROJECT_NAME,
            "assistant_id": str(assistant_id),
            "run_key": receipt.run_key,
        },
        headers=HEADERS,
    )
    assert run_response.status_code == 200, run_response.text
    run = run_response.json()["run"]
    assert run is not None
    assert run["source_type"] == "provider_event"
    assert run["provider_event_receipt_id"] == receipt.receipt_id
    assert run["provider_event_binding_id"] == binding_id
    assert run["provider_event_backend_id"] == "composio"
    assert run["provider_event_app_slug"] == "github"
    assert run["provider_event_slug"] == "github.issue_created"
    assert run["provider_event_schema_version"] == "1"
    assert run["provider_event_acceptance_epoch"] == receipt.acceptance_epoch
    assert run["provider_event_occurred_at"] == payload["timestamp"]
    assert run["provider_event_matched_filters"]
    assert run["provider_event_identity_hmac"] == receipt.provider_event_identity_hmac
    assert run["source_ref"] == payload["id"]
    assert receipt.event_context_expires_at is not None
    assert (
        run["event_context_expires_at"] == receipt.event_context_expires_at.isoformat()
    )


@pytest.mark.anyio
async def test_signed_composio_webhook_with_unmatched_filters_records_ignored_receipt_only(
    dbsession: Session,
    client: AsyncClient,
) -> None:
    ingress_key, binding_id, _, _ = _seed_active_ingress_binding(
        dbsession,
        extra_filters=[
            ProviderEventTriggerFilter(
                field="title",
                operator="contains",
                value="definitely-not-in-fixture-title",
            ),
        ],
    )
    payload = _load_fixture()
    raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")

    response = await client.post(
        f"/v0/webhooks/integrations/composio/{ingress_key}",
        content=raw_body,
        headers=_sign_body(raw_body, webhook_id="msg_unmatched_1"),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ignored"
    assert body["classification_reason"] == "unmatched"

    receipts = (
        dbsession.execute(
            select(ProviderEventReceipt).where(
                ProviderEventReceipt.binding_id == binding_id,
            ),
        )
        .scalars()
        .all()
    )
    assert len(receipts) == 1
    assert receipts[0].processing_state == "ignored"
    assert receipts[0].classification_reason == "unmatched"
    assert receipts[0].stable_envelope_json is None
    assert receipts[0].curated_projection_json is None
    assert receipts[0].event_context_ref is None
    assert receipts[0].event_context_expires_at is None

    dispatches = (
        dbsession.execute(
            select(ProviderEventDispatch).where(
                ProviderEventDispatch.binding_id == binding_id,
            ),
        )
        .scalars()
        .all()
    )
    assert dispatches == []

    blobs = (
        dbsession.execute(
            select(ProviderEventBlob).where(
                ProviderEventBlob.binding_id == binding_id,
            ),
        )
        .scalars()
        .all()
    )
    assert blobs == []


@pytest.mark.anyio
async def test_provider_trigger_webhook_rejects_invalid_signature_without_side_effects(
    dbsession: Session,
    client: AsyncClient,
) -> None:
    ingress_key, binding_id, _, _ = _seed_active_ingress_binding(dbsession)
    payload = _load_fixture()
    raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = _sign_body(raw_body, webhook_id="msg_bad_sig")
    headers["webhook-signature"] = "v1,deadbeef"

    response = await client.post(
        f"/v0/webhooks/integrations/composio/{ingress_key}",
        content=raw_body,
        headers=headers,
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "authentication_failed"
    assert (
        dbsession.execute(
            select(ProviderEventReceipt).where(
                ProviderEventReceipt.binding_id == binding_id,
            ),
        )
        .scalars()
        .all()
        == []
    )
    assert (
        dbsession.execute(
            select(ProviderEventDispatch).where(
                ProviderEventDispatch.binding_id == binding_id,
            ),
        )
        .scalars()
        .all()
        == []
    )
    assert (
        dbsession.execute(
            select(ProviderEventBlob).where(
                ProviderEventBlob.binding_id == binding_id,
            ),
        )
        .scalars()
        .all()
        == []
    )


@pytest.mark.anyio
async def test_provider_trigger_webhook_rate_limits_before_acceptance(
    dbsession: Session,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "provider_trigger_ingress_rate_limit_per_minute", 1)
    reset_ingress_rate_limiter_for_tests()

    ingress_key, binding_id, _, _ = _seed_active_ingress_binding(dbsession)
    payload = _load_fixture()
    raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    path = f"/v0/webhooks/integrations/composio/{ingress_key}"

    first = await client.post(
        path,
        content=raw_body,
        headers=_sign_body(raw_body, webhook_id="msg_rate_1"),
    )
    second = await client.post(
        path,
        content=raw_body,
        headers=_sign_body(raw_body, webhook_id="msg_rate_2"),
    )

    assert first.status_code == 200, first.text
    assert second.status_code == 429
    assert second.json()["detail"] == "rate_limited"

    receipts = (
        dbsession.execute(
            select(ProviderEventReceipt).where(
                ProviderEventReceipt.binding_id == binding_id,
            ),
        )
        .scalars()
        .all()
    )
    assert len(receipts) == 1
