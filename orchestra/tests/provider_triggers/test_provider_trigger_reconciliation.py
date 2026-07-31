"""Integration tests for provider-trigger reconciliation and worker heartbeat."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dao.provider_trigger_dao import ProviderTriggerDAO
from orchestra.db.models.integration_provider_models import IntegrationConnection
from orchestra.db.models.provider_trigger_models import (
    EventTriggerBinding,
    EventTriggerSubscriptionGeneration,
    ProviderTriggerWorkerHeartbeat,
)
from orchestra.provider_triggers.runtime_types import (
    BindingRuntimeHealth,
    DesiredTriggerState,
    GenerationLifecycle,
    GenerationOperationState,
)
from orchestra.provider_triggers.task_trigger import ProviderEventTrigger
from orchestra.provider_triggers.trigger_adapter import (
    NormalizedProviderDelivery,
    ProviderAccountIdentity,
    TriggerDeleteRequest,
    TriggerHealthResult,
    TriggerProviderAdapter,
    TriggerProvisionRequest,
    TriggerProvisionResult,
)
from orchestra.services.provider_trigger_reconciliation_service import (
    ProviderTriggerReconciliationService,
)
from orchestra.settings import settings
from orchestra.workers.provider_trigger_worker import WORKER_KEY, run_worker_cycle


@dataclass
class _RecordedProvision:
    request: TriggerProvisionRequest


class FakeTriggerAdapter(TriggerProviderAdapter):
    """Boundary double for external provider trigger I/O."""

    backend_id = "composio"

    def __init__(
        self,
        *,
        subject: str = "provider-user-1",
        subject_hmac: str = "subject-hmac-test",
        external_trigger_id: str = "ti_fake_123",
        health_status: str = "ok",
        health_error_code: str | None = None,
        provision_error: Exception | None = None,
    ) -> None:
        self.subject = subject
        self.subject_hmac = subject_hmac
        self.external_trigger_id = external_trigger_id
        self.health_status = health_status
        self.health_error_code = health_error_code
        self.provision_error = provision_error
        self.provision_calls: list[_RecordedProvision] = []
        self.delete_calls: list[TriggerDeleteRequest] = []

    def resolve_account_identity(
        self,
        *,
        provider_connection_id: str,
    ) -> ProviderAccountIdentity:
        return ProviderAccountIdentity(
            subject=self.subject,
            display_label=f"github:{self.subject}",
            subject_hmac=self.subject_hmac,
            connected_account_id=provider_connection_id,
            provider_user_id=self.subject,
        )

    def provision(self, request: TriggerProvisionRequest) -> TriggerProvisionResult:
        self.provision_calls.append(_RecordedProvision(request=request))
        if self.provision_error is not None:
            raise self.provision_error
        return TriggerProvisionResult(
            external_trigger_id=self.external_trigger_id,
            signing_secret_ref="env:COMPOSIO_WEBHOOK_SECRET",
            signing_secret_version="project",
        )

    def delete(self, request: TriggerDeleteRequest) -> None:
        self.delete_calls.append(request)

    def verify_delivery(
        self,
        *,
        headers: Mapping[str, str],
        raw_body: bytes,
        signing_secrets: Sequence[str],
        tolerance_seconds: int | None = None,
    ) -> bool:
        _ = headers, raw_body, signing_secrets, tolerance_seconds
        return True

    def normalize_delivery(
        self,
        *,
        headers: Mapping[str, str],
        raw_body: bytes | Mapping[str, Any],
    ) -> NormalizedProviderDelivery:
        _ = headers, raw_body
        raise NotImplementedError

    def stable_event_identity(
        self,
        delivery: Mapping[str, Any] | NormalizedProviderDelivery,
    ) -> str | None:
        _ = delivery
        return "identity-test"

    def health(
        self,
        *,
        external_trigger_id: str | None,
        provider_connection_id: str | None,
        connection_id: str | None = None,
    ) -> TriggerHealthResult:
        _ = external_trigger_id, provider_connection_id, connection_id
        if self.health_status == "ok":
            return TriggerHealthResult(status="ok")
        return TriggerHealthResult(
            status="error",
            error_code=self.health_error_code or "provider_health_check_failed",
        )


def _connection_id() -> str:
    return f"conn-{uuid.uuid4().hex[:10]}"


def _binding_id() -> str:
    return f"binding-{uuid.uuid4().hex[:12]}"


def _seed_connection(
    dbsession: Session,
    *,
    connection_id: str,
    assistant_id: int = 7,
    provider_connection_id: str = "ca_test_123",
    provider_user_id: str = "provider-user-1",
    status: str = "connected",
) -> IntegrationConnection:
    connection = IntegrationConnection(
        connection_id=connection_id,
        owner_scope="assistant",
        assistant_id=assistant_id,
        canonical_app_slug="github",
        backend_id="composio",
        provider_app_id="GITHUB",
        provider_connection_id=provider_connection_id,
        provider_user_id=provider_user_id,
        status=status,
        credential_storage="provider_vault",
    )
    dbsession.add(connection)
    dbsession.flush()
    return connection


def _seed_enabled_binding(
    dbsession: Session,
    *,
    binding_id: str,
    connection_id: str,
    assistant_id: int = 7,
) -> EventTriggerBinding:
    dao = ProviderTriggerDAO(dbsession)
    trigger = ProviderEventTrigger(
        state="enabled",
        connection_id=connection_id,
        backend_id="composio",
        canonical_app_slug="github",
        provider_trigger_slug="GITHUB_ISSUE_CREATED_TRIGGER",
        trigger_config={"owner": "unifyai", "repo": "demo"},
    )
    binding = dao.create_binding(
        binding_id=binding_id,
        project_id=1,
        tasks_context_id=1,
        source_task_log_id=abs(hash(binding_id)) % (2**30),
        task_id=abs(hash(binding_id)) % (2**30),
        assistant_id=assistant_id,
        task_revision=1,
        trigger=trigger,
        execution_mode="live",
        entrypoint=None,
    )
    binding.reconcile_next_retry_at = datetime.now(timezone.utc)
    dbsession.flush()
    return binding


def _service(
    dbsession: Session,
    adapter: FakeTriggerAdapter,
) -> ProviderTriggerReconciliationService:
    return ProviderTriggerReconciliationService(
        dbsession,
        lease_owner="test-worker",
        adapter_resolver=lambda _backend_id: adapter,
    )


def test_reconcile_enabled_binding_provisions_and_promotes_generation(
    dbsession: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "orchestra_trigger_callback_base_url",
        "https://orchestra.example",
    )
    connection_id = _connection_id()
    binding_id = _binding_id()
    _seed_connection(dbsession, connection_id=connection_id)
    _seed_enabled_binding(dbsession, binding_id=binding_id, connection_id=connection_id)
    adapter = FakeTriggerAdapter()
    service = _service(dbsession, adapter)

    reconcile_stats = service.process_reconcile_batch()
    dbsession.commit()
    generation_stats = service.process_generation_batch()
    dbsession.commit()

    assert reconcile_stats["bindings_claimed"] == 1
    assert generation_stats["generations_claimed"] == 1
    assert len(adapter.provision_calls) == 1

    generation = dbsession.execute(
        select(EventTriggerSubscriptionGeneration).where(
            EventTriggerSubscriptionGeneration.binding_id == binding_id,
        ),
    ).scalar_one()
    provisioned = adapter.provision_calls[0].request
    assert provisioned.callback_url.startswith(
        "https://orchestra.example/v0/webhooks/integrations/composio/",
    )
    assert provisioned.ingress_key == generation.ingress_key

    binding = dbsession.execute(
        select(EventTriggerBinding).where(
            EventTriggerBinding.binding_id == binding_id,
        ),
    ).scalar_one()

    assert generation.create_operation_state == GenerationOperationState.succeeded.value
    assert generation.lifecycle_state == GenerationLifecycle.active.value
    assert generation.external_trigger_id == adapter.external_trigger_id
    assert binding.active_generation_id == generation.generation_id
    assert binding.local_acceptance_open is True
    assert binding.runtime_health == BindingRuntimeHealth.healthy.value
    assert binding.provider_account_subject_hmac == adapter.subject_hmac


def test_reconcile_paused_binding_enqueues_and_executes_generation_delete(
    dbsession: Session,
) -> None:
    connection_id = _connection_id()
    binding_id = _binding_id()
    _seed_connection(dbsession, connection_id=connection_id)
    binding = _seed_enabled_binding(
        dbsession,
        binding_id=binding_id,
        connection_id=connection_id,
    )
    dao = ProviderTriggerDAO(dbsession)
    generation = dao.create_generation(binding=binding)
    dao.journal_generation_create(
        generation=generation,
        external_trigger_id="ti_existing_1",
    )
    dao.promote_generation(binding=binding, generation=generation)
    binding.desired_trigger_state = DesiredTriggerState.paused.value
    binding.reconcile_next_retry_at = datetime.now(timezone.utc)
    dbsession.flush()

    adapter = FakeTriggerAdapter()
    service = _service(dbsession, adapter)
    service.process_reconcile_batch()
    dbsession.commit()
    service.process_generation_batch()
    dbsession.commit()

    binding = dbsession.execute(
        select(EventTriggerBinding).where(
            EventTriggerBinding.binding_id == binding_id,
        ),
    ).scalar_one()
    generation = dbsession.execute(
        select(EventTriggerSubscriptionGeneration).where(
            EventTriggerSubscriptionGeneration.generation_id
            == generation.generation_id,
        ),
    ).scalar_one()

    assert binding.local_acceptance_open is False
    assert binding.runtime_health == BindingRuntimeHealth.absent.value
    assert generation.lifecycle_state == GenerationLifecycle.removed.value
    assert generation.delete_operation_state == GenerationOperationState.succeeded.value
    assert binding.active_generation_id is None
    assert len(adapter.delete_calls) == 1


def test_health_batch_probes_and_renews_native_google_subscription(
    dbsession: Session,
) -> None:
    """Real NativeGoogle adapter through process_health_batch renews near expiry."""

    from orchestra.provider_triggers.native_google_trigger_adapter import (
        NativeGoogleTriggerAdapter,
    )
    from orchestra.provider_triggers.workspace_trigger_credentials import (
        WorkspaceTriggerCredentials,
    )

    connection_id = _connection_id()
    binding_id = _binding_id()
    subscription_name = "subscriptions/healthRenewAbcd"
    expire_soon = (datetime.now(timezone.utc) + timedelta(hours=6)).strftime(
        "%Y-%m-%dT%H:%M:%SZ",
    )
    http_calls: list[tuple[str, str, dict[str, Any]]] = []
    credential_calls: list[str] = []

    class _Loader:
        def load_for_connection_id(
            self,
            requested_connection_id: str,
        ) -> WorkspaceTriggerCredentials:
            credential_calls.append(requested_connection_id)
            return WorkspaceTriggerCredentials(
                connection_id=requested_connection_id,
                provider_connection_id="google:meet.user@example.com",
                account_email="meet.user@example.com",
                access_token="ya29.health-token",
                refresh_token="refresh",
                granted_scopes=(),
                secret_values={},
            )

    def request_fn(method: str, url: str, **kwargs: Any) -> Any:
        http_calls.append((method, url, kwargs))

        class _Response:
            status_code = 200
            text = "{}"

            def json(self) -> dict[str, Any]:
                if method == "GET":
                    return {
                        "name": subscription_name,
                        "state": "ACTIVE",
                        "expireTime": expire_soon,
                    }
                return {
                    "done": True,
                    "response": {
                        "name": subscription_name,
                        "state": "ACTIVE",
                        "expireTime": (
                            datetime.now(timezone.utc) + timedelta(days=6)
                        ).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    },
                }

            def raise_for_status(self) -> None:
                return None

        return _Response()

    _seed_connection(
        dbsession,
        connection_id=connection_id,
        provider_connection_id="google:meet.user@example.com",
    )
    binding = _seed_enabled_binding(
        dbsession,
        binding_id=binding_id,
        connection_id=connection_id,
    )
    dao = ProviderTriggerDAO(dbsession)
    generation = dao.create_generation(binding=binding)
    dao.journal_generation_create(
        generation=generation,
        external_trigger_id=subscription_name,
    )
    dao.promote_generation(binding=binding, generation=generation)
    binding.runtime_health = BindingRuntimeHealth.healthy.value
    binding.last_health_check_at = None
    dbsession.flush()

    adapter = NativeGoogleTriggerAdapter(
        credential_loader=_Loader(),  # type: ignore[arg-type]
        webhook_secret="native-google-secret",
        pubsub_topic="projects/test/topics/workspace-events",
        request_fn=request_fn,
    )
    service = ProviderTriggerReconciliationService(
        dbsession,
        lease_owner="test-worker",
        adapter_resolver=lambda _backend_id: adapter,
    )
    service.process_health_batch()
    dbsession.commit()

    binding = dbsession.execute(
        select(EventTriggerBinding).where(
            EventTriggerBinding.binding_id == binding_id,
        ),
    ).scalar_one()
    assert binding.runtime_health == BindingRuntimeHealth.healthy.value
    assert binding.consecutive_health_failures == 0
    assert credential_calls == [connection_id]
    assert http_calls[0][0] == "GET"
    assert http_calls[0][1].endswith(f"/{subscription_name}")
    patch_calls = [call for call in http_calls if call[0] == "PATCH"]
    assert len(patch_calls) == 1
    assert "updateMask=ttl" in patch_calls[0][1]
    assert patch_calls[0][2]["json"] == {"ttl": "0s"}


def test_reconcile_does_not_clear_needs_attention_for_active_generation(
    dbsession: Session,
) -> None:
    """A dead remote subscription must stay Needs attention until reprovision."""

    connection_id = _connection_id()
    binding_id = _binding_id()
    _seed_connection(dbsession, connection_id=connection_id)
    binding = _seed_enabled_binding(
        dbsession,
        binding_id=binding_id,
        connection_id=connection_id,
    )
    dao = ProviderTriggerDAO(dbsession)
    generation = dao.create_generation(binding=binding)
    dao.journal_generation_create(
        generation=generation,
        external_trigger_id="subscriptions/deadRemoteSub",
    )
    dao.promote_generation(binding=binding, generation=generation)
    binding.runtime_health = BindingRuntimeHealth.needs_attention.value
    binding.last_stable_error_code = "provider_subscription_missing"
    binding.consecutive_health_failures = 3
    binding.local_acceptance_open = False
    binding.reconcile_next_retry_at = datetime.now(timezone.utc)
    dbsession.flush()

    adapter = FakeTriggerAdapter(health_status="ok")
    service = _service(dbsession, adapter)
    service.process_reconcile_batch()
    dbsession.commit()

    binding = dbsession.execute(
        select(EventTriggerBinding).where(
            EventTriggerBinding.binding_id == binding_id,
        ),
    ).scalar_one()
    assert binding.runtime_health == BindingRuntimeHealth.needs_attention.value
    assert binding.last_stable_error_code == "provider_subscription_missing"
    assert binding.local_acceptance_open is False


def test_health_failures_increment_counter_and_trip_threshold(
    dbsession: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "provider_trigger_health_failure_threshold", 2)
    connection_id = _connection_id()
    binding_id = _binding_id()
    _seed_connection(dbsession, connection_id=connection_id)
    binding = _seed_enabled_binding(
        dbsession,
        binding_id=binding_id,
        connection_id=connection_id,
    )
    dao = ProviderTriggerDAO(dbsession)
    generation = dao.create_generation(binding=binding)
    dao.journal_generation_create(
        generation=generation,
        external_trigger_id="ti_existing_2",
    )
    dao.promote_generation(binding=binding, generation=generation)
    binding.runtime_health = BindingRuntimeHealth.healthy.value
    binding.last_health_check_at = None
    dbsession.flush()

    adapter = FakeTriggerAdapter(
        health_status="error",
        health_error_code="provider_connection_not_active",
    )
    service = _service(dbsession, adapter)

    service.process_health_batch()
    dbsession.commit()
    binding = dbsession.execute(
        select(EventTriggerBinding).where(
            EventTriggerBinding.binding_id == binding_id,
        ),
    ).scalar_one()
    assert binding.consecutive_health_failures == 1
    assert binding.runtime_health == BindingRuntimeHealth.recovering.value
    assert binding.last_stable_error_code == "provider_connection_not_active"
    assert binding.last_health_check_at is not None

    binding.last_health_check_at = datetime.now(timezone.utc) - timedelta(
        seconds=settings.provider_trigger_health_interval_seconds + 1,
    )
    dbsession.flush()

    service.process_health_batch()
    dbsession.commit()
    binding = dbsession.execute(
        select(EventTriggerBinding).where(
            EventTriggerBinding.binding_id == binding_id,
        ),
    ).scalar_one()
    assert binding.consecutive_health_failures == 2
    assert binding.runtime_health == BindingRuntimeHealth.needs_attention.value
    assert binding.local_acceptance_open is False
    assert binding.reconcile_next_retry_at is not None


def test_health_check_fails_when_local_connection_not_active(
    dbsession: Session,
) -> None:
    """A disconnected local connection must fail health even though the
    provider-side adapter would otherwise report the trigger as healthy."""

    connection_id = _connection_id()
    binding_id = _binding_id()
    _seed_connection(dbsession, connection_id=connection_id, status="disconnected")
    binding = _seed_enabled_binding(
        dbsession,
        binding_id=binding_id,
        connection_id=connection_id,
    )
    dao = ProviderTriggerDAO(dbsession)
    generation = dao.create_generation(binding=binding)
    dao.journal_generation_create(
        generation=generation,
        external_trigger_id="ti_existing_3",
    )
    dao.promote_generation(binding=binding, generation=generation)
    binding.runtime_health = BindingRuntimeHealth.healthy.value
    binding.last_health_check_at = None
    dbsession.flush()

    adapter = FakeTriggerAdapter(health_status="ok")
    service = _service(dbsession, adapter)

    service.process_health_batch()
    dbsession.commit()

    binding = dbsession.execute(
        select(EventTriggerBinding).where(
            EventTriggerBinding.binding_id == binding_id,
        ),
    ).scalar_one()
    assert binding.consecutive_health_failures == 1
    assert binding.runtime_health == BindingRuntimeHealth.recovering.value
    assert binding.last_stable_error_code == "connection_not_active"
    assert binding.last_health_check_at is not None
    assert adapter.provision_calls == []


def test_health_check_passes_with_active_local_connection(
    dbsession: Session,
) -> None:
    """Provider-side health signal is still respected when the local
    connection status is active."""

    connection_id = _connection_id()
    binding_id = _binding_id()
    _seed_connection(dbsession, connection_id=connection_id, status="connected")
    binding = _seed_enabled_binding(
        dbsession,
        binding_id=binding_id,
        connection_id=connection_id,
    )
    dao = ProviderTriggerDAO(dbsession)
    generation = dao.create_generation(binding=binding)
    dao.journal_generation_create(
        generation=generation,
        external_trigger_id="ti_existing_4",
    )
    dao.promote_generation(binding=binding, generation=generation)
    binding.runtime_health = BindingRuntimeHealth.healthy.value
    binding.last_health_check_at = None
    dbsession.flush()

    adapter = FakeTriggerAdapter(health_status="ok")
    service = _service(dbsession, adapter)

    service.process_health_batch()
    dbsession.commit()

    binding = dbsession.execute(
        select(EventTriggerBinding).where(
            EventTriggerBinding.binding_id == binding_id,
        ),
    ).scalar_one()
    assert binding.consecutive_health_failures == 0
    assert binding.runtime_health == BindingRuntimeHealth.healthy.value
    assert binding.last_stable_error_code is None


def test_claim_generations_for_operation_reclaims_expired_lease(
    dbsession: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "orchestra_trigger_callback_base_url",
        "https://orchestra.example",
    )
    connection_id = _connection_id()
    binding_id = _binding_id()
    _seed_connection(dbsession, connection_id=connection_id)
    binding = _seed_enabled_binding(
        dbsession,
        binding_id=binding_id,
        connection_id=connection_id,
    )
    dao = ProviderTriggerDAO(dbsession)
    generation = dao.create_generation(binding=binding)
    generation.create_operation_state = GenerationOperationState.claimed.value
    generation.lease_owner = "stale-owner"
    generation.lease_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    generation.next_retry_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    dbsession.flush()

    adapter = FakeTriggerAdapter()
    service = _service(dbsession, adapter)
    stats = service.process_generation_batch()
    dbsession.commit()

    refreshed = dbsession.execute(
        select(EventTriggerSubscriptionGeneration).where(
            EventTriggerSubscriptionGeneration.generation_id
            == generation.generation_id,
        ),
    ).scalar_one()

    assert stats["generations_claimed"] == 1
    assert refreshed.create_operation_state == GenerationOperationState.succeeded.value
    assert refreshed.lifecycle_state == GenerationLifecycle.active.value
    assert refreshed.lease_owner is None
    assert len(adapter.provision_calls) == 1


def test_run_worker_cycle_upserts_heartbeat_metadata(
    dbsession: Session,
) -> None:
    totals_first = run_worker_cycle(lease_owner="test-owner")
    totals_second = run_worker_cycle(lease_owner="test-owner")

    heartbeat = dbsession.execute(
        select(ProviderTriggerWorkerHeartbeat).where(
            ProviderTriggerWorkerHeartbeat.worker_key == WORKER_KEY,
        ),
    ).scalar_one()

    assert totals_first["bindings_claimed"] == 0
    assert totals_second["bindings_claimed"] == 0
    assert "dispatches_claimed" in totals_second
    assert "dispatch_backlog_oldest_age_seconds" in totals_second
    assert heartbeat.lease_owner == "test-owner"
    assert heartbeat.metadata_json == totals_second
    assert heartbeat.last_reconcile_at is not None
    assert heartbeat.last_health_at is not None
    assert heartbeat.last_heartbeat_at is not None
