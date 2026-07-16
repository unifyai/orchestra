"""Local Composio stub control-plane journeys over the real Orchestra stack."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.models.provider_trigger_models import (
    EventTriggerBinding,
    EventTriggerSubscriptionGeneration,
    ProviderEventBlob,
    ProviderEventDispatch,
    ProviderEventReceipt,
)
from orchestra.provider_triggers.local_composio_trigger_adapter import (
    LocalComposioTriggerAdapter,
    get_local_composio_trigger_scenario,
)
from orchestra.provider_triggers.runtime_types import DesiredTriggerState
from orchestra.provider_triggers.trigger_adapter_registry import (
    get_trigger_provider_adapter,
)
from orchestra.services.provider_trigger_reconciliation_service import (
    ProviderTriggerReconciliationService,
)
from orchestra.settings import settings
from orchestra.tests.provider_triggers.composio_delivery import (
    deliver_signed_composio_webhook,
    load_composio_github_issue_fixture,
    serialize_composio_payload,
    sign_composio_payload,
)
from orchestra.tests.provider_triggers.conftest import (
    stub_healthy_provider_trigger_topology,
)
from orchestra.tests.provider_triggers.control_plane_harness import (
    WEBHOOK_SECRET,
    apply_signing_overlap,
    create_assistant,
    pause_binding,
    reconcile_binding_to_active_generation,
    run_trigger_worker_cycle,
    seed_integration_connection,
    seed_provider_event_binding,
)
from orchestra.tests.utils import HEADERS
from orchestra.workers.provider_trigger_worker import run_worker_cycle


@pytest.fixture(autouse=True)
def local_composio_stub_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the inbound Composio adapter onto the local stub."""

    monkeypatch.delenv("COMPOSIO_API_KEY", raising=False)
    monkeypatch.setenv("COMPOSIO_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setattr(
        settings,
        "orchestra_trigger_callback_base_url",
        "https://orchestra.example",
    )


def test_registry_returns_local_stub_without_api_key() -> None:
    adapter = get_trigger_provider_adapter("composio")
    assert isinstance(adapter, LocalComposioTriggerAdapter)


def test_local_stub_provision_is_idempotent_after_lost_response(
    dbsession: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_healthy_provider_trigger_topology(monkeypatch)
    assistant_id = 701
    connection = seed_integration_connection(
        dbsession,
        assistant_id=assistant_id,
    )
    binding = seed_provider_event_binding(
        dbsession,
        assistant_id=assistant_id,
        connection_id=connection.connection_id,
    )
    scenario = get_local_composio_trigger_scenario()
    scenario.lose_next_provision_response = True

    service = ProviderTriggerReconciliationService(
        dbsession,
        lease_owner="test-worker",
    )
    service.process_reconcile_batch()
    dbsession.commit()
    service.process_generation_batch()
    dbsession.commit()

    generation = dbsession.execute(
        select(EventTriggerSubscriptionGeneration).where(
            EventTriggerSubscriptionGeneration.binding_id == binding.binding_id,
        ),
    ).scalar_one()
    generation.next_retry_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    dbsession.flush()

    service.process_generation_batch()
    dbsession.commit()

    binding = dbsession.execute(
        select(EventTriggerBinding).where(
            EventTriggerBinding.binding_id == binding.binding_id,
        ),
    ).scalar_one()
    assert binding.active_generation_id is not None
    assert len(scenario.provisions_by_idempotency_key) == 1


@pytest.mark.anyio
async def test_stub_signed_delivery_match_creates_one_run(
    dbsession: Session,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_healthy_provider_trigger_topology(monkeypatch)
    assistant_id = await create_assistant(client)
    connection = seed_integration_connection(
        dbsession,
        assistant_id=assistant_id,
    )
    binding = seed_provider_event_binding(
        dbsession,
        assistant_id=assistant_id,
        connection_id=connection.connection_id,
    )
    generation = reconcile_binding_to_active_generation(
        dbsession,
        binding_id=binding.binding_id,
    )

    payload = load_composio_github_issue_fixture(
        external_trigger_id=generation.external_trigger_id,
        connected_account_id=connection.provider_connection_id,
        provider_user_id=connection.provider_user_id,
    )
    first = await deliver_signed_composio_webhook(
        client,
        ingress_key=generation.ingress_key,
        payload=payload,
        signing_secret=WEBHOOK_SECRET,
        webhook_id="stub_match_1",
    )
    second = await deliver_signed_composio_webhook(
        client,
        ingress_key=generation.ingress_key,
        payload=payload,
        signing_secret=WEBHOOK_SECRET,
        webhook_id="stub_match_2",
    )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["receipt_id"] == second.json()["receipt_id"]

    receipts = (
        dbsession.execute(
            select(ProviderEventReceipt).where(
                ProviderEventReceipt.binding_id == binding.binding_id,
            ),
        )
        .scalars()
        .all()
    )
    dispatches = (
        dbsession.execute(
            select(ProviderEventDispatch).where(
                ProviderEventDispatch.binding_id == binding.binding_id,
            ),
        )
        .scalars()
        .all()
    )
    assert len(receipts) == 1
    assert len(dispatches) == 1


@pytest.mark.anyio
async def test_stub_unauthorized_connected_account_records_ignored_receipt_only(
    dbsession: Session,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_healthy_provider_trigger_topology(monkeypatch)
    assistant_id = await create_assistant(client)
    connection = seed_integration_connection(
        dbsession,
        assistant_id=assistant_id,
    )
    binding = seed_provider_event_binding(
        dbsession,
        assistant_id=assistant_id,
        connection_id=connection.connection_id,
    )
    generation = reconcile_binding_to_active_generation(
        dbsession,
        binding_id=binding.binding_id,
    )

    payload = load_composio_github_issue_fixture(
        external_trigger_id=generation.external_trigger_id,
        connected_account_id="ca_wrong_account",
        provider_user_id=connection.provider_user_id,
    )
    response = await deliver_signed_composio_webhook(
        client,
        ingress_key=generation.ingress_key,
        payload=payload,
        signing_secret=WEBHOOK_SECRET,
        webhook_id="stub_unmatched_1",
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ignored"

    receipt = dbsession.execute(
        select(ProviderEventReceipt).where(
            ProviderEventReceipt.binding_id == binding.binding_id,
        ),
    ).scalar_one()
    assert receipt.stable_envelope_json is None
    assert receipt.event_context_ref is None
    assert (
        dbsession.execute(
            select(ProviderEventDispatch).where(
                ProviderEventDispatch.binding_id == binding.binding_id,
            ),
        )
        .scalars()
        .all()
        == []
    )
    assert (
        dbsession.execute(
            select(ProviderEventBlob).where(
                ProviderEventBlob.binding_id == binding.binding_id,
            ),
        )
        .scalars()
        .all()
        == []
    )


@pytest.mark.anyio
async def test_paused_binding_rejects_new_deliveries(
    dbsession: Session,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_healthy_provider_trigger_topology(monkeypatch)
    assistant_id = await create_assistant(client)
    connection = seed_integration_connection(
        dbsession,
        assistant_id=assistant_id,
    )
    binding = seed_provider_event_binding(
        dbsession,
        assistant_id=assistant_id,
        connection_id=connection.connection_id,
    )
    generation = reconcile_binding_to_active_generation(
        dbsession,
        binding_id=binding.binding_id,
    )
    binding = dbsession.execute(
        select(EventTriggerBinding).where(
            EventTriggerBinding.binding_id == binding.binding_id,
        ),
    ).scalar_one()
    pause_binding(dbsession, binding)
    run_worker_cycle(lease_owner="test-worker")
    dbsession.commit()

    payload = load_composio_github_issue_fixture(
        external_trigger_id=generation.external_trigger_id,
        connected_account_id=connection.provider_connection_id,
        provider_user_id=connection.provider_user_id,
    )
    response = await deliver_signed_composio_webhook(
        client,
        ingress_key=generation.ingress_key,
        payload=payload,
        signing_secret=WEBHOOK_SECRET,
        webhook_id="stub_paused_1",
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ignored"
    assert (
        dbsession.execute(
            select(ProviderEventDispatch).where(
                ProviderEventDispatch.binding_id == binding.binding_id,
            ),
        )
        .scalars()
        .all()
        == []
    )


@pytest.mark.anyio
async def test_unauthorized_connected_account_delivery_is_ignored(
    dbsession: Session,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_healthy_provider_trigger_topology(monkeypatch)
    assistant_id = await create_assistant(client)
    connection = seed_integration_connection(
        dbsession,
        assistant_id=assistant_id,
    )
    binding = seed_provider_event_binding(
        dbsession,
        assistant_id=assistant_id,
        connection_id=connection.connection_id,
    )
    generation = reconcile_binding_to_active_generation(
        dbsession,
        binding_id=binding.binding_id,
    )

    payload = load_composio_github_issue_fixture(
        external_trigger_id=generation.external_trigger_id,
        connected_account_id="ca_wrong_account",
        provider_user_id=connection.provider_user_id,
    )
    response = await deliver_signed_composio_webhook(
        client,
        ingress_key=generation.ingress_key,
        payload=payload,
        signing_secret=WEBHOOK_SECRET,
        webhook_id="stub_unauthorized_1",
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ignored"
    assert response.json()["classification_reason"] == "unauthorized"
    assert (
        dbsession.execute(
            select(ProviderEventDispatch).where(
                ProviderEventDispatch.binding_id == binding.binding_id,
            ),
        )
        .scalars()
        .all()
        == []
    )


@pytest.mark.anyio
async def test_signing_overlap_accepts_previous_secret(
    dbsession: Session,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_healthy_provider_trigger_topology(monkeypatch)
    assistant_id = await create_assistant(client)
    connection = seed_integration_connection(
        dbsession,
        assistant_id=assistant_id,
    )
    binding = seed_provider_event_binding(
        dbsession,
        assistant_id=assistant_id,
        connection_id=connection.connection_id,
    )
    generation = reconcile_binding_to_active_generation(
        dbsession,
        binding_id=binding.binding_id,
    )
    previous_secret = "previous-overlap-secret"
    apply_signing_overlap(generation, previous_secret=previous_secret)
    dbsession.flush()

    payload = load_composio_github_issue_fixture(
        external_trigger_id=generation.external_trigger_id,
        connected_account_id=connection.provider_connection_id,
        provider_user_id=connection.provider_user_id,
    )
    raw_body = serialize_composio_payload(payload)
    response = await client.post(
        f"/v0/webhooks/integrations/composio/{generation.ingress_key}",
        content=raw_body,
        headers=sign_composio_payload(
            raw_body,
            signing_secret=previous_secret,
            webhook_id="stub_overlap_1",
        ),
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "accepted"


@pytest.mark.anyio
async def test_typed_task_enable_reconciles_through_local_stub(
    client: AsyncClient,
    dbsession: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_healthy_provider_trigger_topology(monkeypatch)
    assistant_id = await create_assistant(client)
    connection = seed_integration_connection(
        dbsession,
        assistant_id=assistant_id,
        connection_id=f"conn-{assistant_id}-github",
    )

    response = await client.post(
        f"/v0/assistants/{assistant_id}/tasks",
        json={
            "name": "GitHub issue triage",
            "description": "Triage new GitHub issues.",
            "status": "triggerable",
            "trigger": {
                "kind": "provider_event",
                "state": "enabled",
                "connection_id": connection.connection_id,
                "backend_id": "composio",
                "canonical_app_slug": "github",
                "provider_trigger_slug": "GITHUB_ISSUE_CREATED_TRIGGER",
                "trigger_config": {
                    "owner": "octocat",
                    "repo": "Hello-World",
                },
            },
            "enabled": True,
            "offline": False,
            "priority": "normal",
        },
        headers=HEADERS,
    )
    assert response.status_code == 201, response.text
    created = response.json()["info"]
    run_trigger_worker_cycle()
    dbsession.expire_all()

    binding = dbsession.execute(
        select(EventTriggerBinding).where(
            EventTriggerBinding.binding_id == created["provider_event_binding_id"],
        ),
    ).scalar_one()
    assert binding.runtime_health in {"healthy", "provisioning", "recovering"}
    assert binding.desired_trigger_state == DesiredTriggerState.enabled.value


@pytest.mark.anyio
async def test_account_subject_change_surfaces_needs_attention_health(
    client: AsyncClient,
    dbsession: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_healthy_provider_trigger_topology(monkeypatch)
    scenario = get_local_composio_trigger_scenario()

    assistant_id = await create_assistant(client)
    connection = seed_integration_connection(
        dbsession,
        assistant_id=assistant_id,
    )
    binding = seed_provider_event_binding(
        dbsession,
        assistant_id=assistant_id,
        connection_id=connection.connection_id,
    )
    reconcile_binding_to_active_generation(
        dbsession,
        binding_id=binding.binding_id,
    )
    binding = dbsession.execute(
        select(EventTriggerBinding).where(
            EventTriggerBinding.binding_id == binding.binding_id,
        ),
    ).scalar_one()
    assert binding.provider_account_subject_hmac

    scenario.alternate_subject = "assistant:rotated-provider-subject"
    binding.reconcile_next_retry_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    dbsession.flush()

    service = ProviderTriggerReconciliationService(
        dbsession,
        lease_owner="test-worker",
    )
    service.process_reconcile_batch()
    dbsession.commit()

    binding = dbsession.execute(
        select(EventTriggerBinding).where(
            EventTriggerBinding.binding_id == binding.binding_id,
        ),
    ).scalar_one()
    assert binding.runtime_health == "needs_attention"
    assert binding.last_stable_error_code == "account_subject_mismatch"
    assert binding.local_acceptance_open is False


def test_pause_enqueues_stub_delete_and_closes_acceptance(
    dbsession: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_healthy_provider_trigger_topology(monkeypatch)
    scenario = get_local_composio_trigger_scenario()
    assistant_id = 702
    connection = seed_integration_connection(
        dbsession,
        assistant_id=assistant_id,
    )
    binding = seed_provider_event_binding(
        dbsession,
        assistant_id=assistant_id,
        connection_id=connection.connection_id,
    )
    generation = reconcile_binding_to_active_generation(
        dbsession,
        binding_id=binding.binding_id,
    )
    binding = dbsession.execute(
        select(EventTriggerBinding).where(
            EventTriggerBinding.binding_id == binding.binding_id,
        ),
    ).scalar_one()
    pause_binding(dbsession, binding)

    service = ProviderTriggerReconciliationService(
        dbsession,
        lease_owner="test-worker",
    )
    service.process_reconcile_batch()
    dbsession.commit()
    service.process_generation_batch()
    dbsession.commit()

    binding = dbsession.execute(
        select(EventTriggerBinding).where(
            EventTriggerBinding.binding_id == binding.binding_id,
        ),
    ).scalar_one()
    assert binding.local_acceptance_open is False
    assert len(scenario.delete_calls) == 1
    assert (
        scenario.delete_calls[0].external_trigger_id == generation.external_trigger_id
    )
