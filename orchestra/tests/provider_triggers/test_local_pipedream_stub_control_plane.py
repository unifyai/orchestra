"""Local Pipedream stub control-plane journeys over the real Orchestra stack."""

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
from orchestra.provider_triggers.local_pipedream_trigger_adapter import (
    LocalPipedreamTriggerAdapter,
    get_local_pipedream_trigger_scenario,
)
from orchestra.provider_triggers.signing_secret_refs import resolve_signing_secret_ref
from orchestra.provider_triggers.signing_secret_storage import (
    wrap_signing_secret_for_generation,
)
from orchestra.provider_triggers.trigger_adapter_registry import (
    get_trigger_provider_adapter,
)
from orchestra.services.provider_trigger_reconciliation_service import (
    ProviderTriggerReconciliationService,
)
from orchestra.settings import settings
from orchestra.tests.provider_triggers.conftest import (
    stub_healthy_provider_trigger_topology,
)
from orchestra.tests.provider_triggers.control_plane_harness import (
    apply_signing_overlap,
    create_assistant,
    pause_binding,
    reconcile_binding_to_active_generation,
    seed_integration_connection,
    seed_provider_event_binding,
)
from orchestra.tests.provider_triggers.pipedream_delivery import (
    deliver_signed_pipedream_webhook,
    load_pipedream_github_issue_fixture,
)
from orchestra.workers.provider_trigger_worker import run_worker_cycle


@pytest.fixture(autouse=True)
def local_pipedream_stub_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the inbound Pipedream adapter onto the local stub."""

    monkeypatch.delenv("PIPEDREAM_CLIENT_ID", raising=False)
    monkeypatch.delenv("PIPEDREAM_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("PIPEDREAM_PROJECT_ID", raising=False)
    monkeypatch.setattr(
        settings,
        "orchestra_trigger_callback_base_url",
        "https://orchestra.example",
    )


def test_registry_returns_local_pipedream_stub_without_credentials() -> None:
    adapter = get_trigger_provider_adapter("pipedream")
    assert isinstance(adapter, LocalPipedreamTriggerAdapter)


def test_local_pipedream_stub_provision_is_idempotent_after_lost_response(
    dbsession: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_healthy_provider_trigger_topology(monkeypatch)
    assistant_id = 801
    connection = seed_integration_connection(
        dbsession,
        assistant_id=assistant_id,
        backend_id="pipedream",
    )
    binding = seed_provider_event_binding(
        dbsession,
        assistant_id=assistant_id,
        connection_id=connection.connection_id,
        backend_id="pipedream",
    )
    scenario = get_local_pipedream_trigger_scenario()
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
async def test_pipedream_stub_signed_delivery_match_creates_one_run(
    dbsession: Session,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_healthy_provider_trigger_topology(monkeypatch)
    assistant_id = await create_assistant(client)
    connection = seed_integration_connection(
        dbsession,
        assistant_id=assistant_id,
        backend_id="pipedream",
    )
    binding = seed_provider_event_binding(
        dbsession,
        assistant_id=assistant_id,
        connection_id=connection.connection_id,
        backend_id="pipedream",
    )
    generation = reconcile_binding_to_active_generation(
        dbsession,
        binding_id=binding.binding_id,
    )
    signing_secret = resolve_signing_secret_ref(generation.signing_secret_ref)
    assert signing_secret

    payload = load_pipedream_github_issue_fixture()
    first = await deliver_signed_pipedream_webhook(
        client,
        ingress_key=generation.ingress_key,
        payload=payload,
        signing_secret=signing_secret,
    )
    second = await deliver_signed_pipedream_webhook(
        client,
        ingress_key=generation.ingress_key,
        payload=payload,
        signing_secret=signing_secret,
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
async def test_pipedream_stub_paused_binding_records_ignored_receipt_only(
    dbsession: Session,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_healthy_provider_trigger_topology(monkeypatch)
    assistant_id = await create_assistant(client)
    connection = seed_integration_connection(
        dbsession,
        assistant_id=assistant_id,
        backend_id="pipedream",
    )
    binding = seed_provider_event_binding(
        dbsession,
        assistant_id=assistant_id,
        connection_id=connection.connection_id,
        backend_id="pipedream",
    )
    generation = reconcile_binding_to_active_generation(
        dbsession,
        binding_id=binding.binding_id,
    )
    pause_binding(dbsession, binding)
    signing_secret = resolve_signing_secret_ref(generation.signing_secret_ref)
    assert signing_secret

    payload = load_pipedream_github_issue_fixture()
    response = await deliver_signed_pipedream_webhook(
        client,
        ingress_key=generation.ingress_key,
        payload=payload,
        signing_secret=signing_secret,
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
async def test_pipedream_stub_non_opened_delivery_is_accepted(
    dbsession: Session,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_healthy_provider_trigger_topology(monkeypatch)
    assistant_id = await create_assistant(client)
    connection = seed_integration_connection(
        dbsession,
        assistant_id=assistant_id,
        backend_id="pipedream",
    )
    binding = seed_provider_event_binding(
        dbsession,
        assistant_id=assistant_id,
        connection_id=connection.connection_id,
        backend_id="pipedream",
    )
    generation = reconcile_binding_to_active_generation(
        dbsession,
        binding_id=binding.binding_id,
    )
    signing_secret = resolve_signing_secret_ref(generation.signing_secret_ref)
    assert signing_secret

    payload = load_pipedream_github_issue_fixture(action="closed")
    response = await deliver_signed_pipedream_webhook(
        client,
        ingress_key=generation.ingress_key,
        payload=payload,
        signing_secret=signing_secret,
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "accepted"
    assert (
        dbsession.execute(
            select(ProviderEventDispatch).where(
                ProviderEventDispatch.binding_id == binding.binding_id,
            ),
        )
        .scalars()
        .all()
    )


@pytest.mark.anyio
async def test_pipedream_paused_binding_rejects_new_deliveries(
    dbsession: Session,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_healthy_provider_trigger_topology(monkeypatch)
    assistant_id = await create_assistant(client)
    connection = seed_integration_connection(
        dbsession,
        assistant_id=assistant_id,
        backend_id="pipedream",
    )
    binding = seed_provider_event_binding(
        dbsession,
        assistant_id=assistant_id,
        connection_id=connection.connection_id,
        backend_id="pipedream",
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

    signing_secret = resolve_signing_secret_ref(generation.signing_secret_ref)
    assert signing_secret
    payload = load_pipedream_github_issue_fixture()
    response = await deliver_signed_pipedream_webhook(
        client,
        ingress_key=generation.ingress_key,
        payload=payload,
        signing_secret=signing_secret,
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
async def test_pipedream_previous_signing_key_accepted_during_overlap(
    dbsession: Session,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_healthy_provider_trigger_topology(monkeypatch)
    assistant_id = await create_assistant(client)
    connection = seed_integration_connection(
        dbsession,
        assistant_id=assistant_id,
        backend_id="pipedream",
    )
    binding = seed_provider_event_binding(
        dbsession,
        assistant_id=assistant_id,
        connection_id=connection.connection_id,
        backend_id="pipedream",
    )
    generation = reconcile_binding_to_active_generation(
        dbsession,
        binding_id=binding.binding_id,
    )
    previous_secret = "pd-overlap-previous-secret"
    previous_ref, _ = wrap_signing_secret_for_generation(
        generation_id=f"{generation.generation_id}-prev",
        signing_key=previous_secret,
    )
    apply_signing_overlap(
        generation,
        previous_secret=previous_ref,
    )
    dbsession.flush()

    payload = load_pipedream_github_issue_fixture(trace_id="pd_overlap_delivery")
    response = await deliver_signed_pipedream_webhook(
        client,
        ingress_key=generation.ingress_key,
        payload=payload,
        signing_secret=previous_secret,
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "accepted"
