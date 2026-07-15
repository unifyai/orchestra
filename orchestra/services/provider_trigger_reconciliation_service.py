"""Reconcile provider-event bindings and subscription generations."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy.orm import Session

from orchestra.db.dao.integration_provider_dao import IntegrationProviderDAO
from orchestra.db.dao.provider_trigger_dao import ProviderTriggerDAO
from orchestra.db.models.integration_provider_models import IntegrationConnection
from orchestra.db.models.provider_trigger_models import (
    EventTriggerBinding,
    EventTriggerSubscriptionGeneration,
)
from orchestra.provider_triggers.composio_trigger_adapter import (
    github_resource_from_filters,
)
from orchestra.provider_triggers.runtime_types import (
    BindingRuntimeHealth,
    DesiredTriggerState,
    GenerationLifecycle,
    GenerationOperationState,
    ReconcileErrorCode,
)
from orchestra.provider_triggers.trigger_adapter import (
    TriggerDeleteRequest,
    TriggerProviderAdapter,
    TriggerProvisionRequest,
)
from orchestra.provider_triggers.trigger_adapter_registry import (
    get_trigger_provider_adapter,
)
from orchestra.settings import settings

logger = logging.getLogger(__name__)

BASE_RETRY_DELAY_MINUTES = 5
MAX_RETRY_DELAY_MINUTES = 60
ACTIVE_CONNECTION_STATUSES = {"connected", "active"}


class ProviderTriggerReconciliationService:
    """Turn projected binding desired state into provider subscriptions."""

    def __init__(
        self,
        session: Session,
        *,
        lease_owner: str | None = None,
        adapter_resolver: Callable[[str], TriggerProviderAdapter] | None = None,
    ) -> None:
        self._session = session
        self._dao = ProviderTriggerDAO(session)
        self._integration_dao = IntegrationProviderDAO(session)
        self._lease_owner = lease_owner or f"trigger-worker-{uuid.uuid4().hex[:8]}"
        self._adapter_resolver = adapter_resolver or get_trigger_provider_adapter

    @property
    def lease_owner(self) -> str:
        return self._lease_owner

    def process_reconcile_batch(self) -> dict[str, int]:
        """Claim and reconcile one batch of bindings."""

        bindings = self._dao.claim_bindings_for_reconcile(
            lease_owner=self._lease_owner,
            limit=settings.provider_trigger_reconcile_batch_size,
            lease_ttl=timedelta(
                seconds=settings.provider_trigger_reconcile_lease_seconds,
            ),
        )
        if bindings:
            self._session.commit()

        processed = 0
        for binding in bindings:
            try:
                self._reconcile_binding(binding)
                processed += 1
            except Exception:
                logger.exception(
                    "provider-trigger binding reconcile failed binding_id=%s",
                    binding.binding_id,
                )
                self._mark_binding_retryable(
                    binding,
                    error_code=ReconcileErrorCode.provider_provision_failed.value,
                )
            self._session.commit()
        return {"bindings_claimed": len(bindings), "bindings_processed": processed}

    def process_generation_batch(self) -> dict[str, int]:
        """Claim and execute one batch of generation create/delete operations."""

        generations = self._dao.claim_generations_for_operation(
            lease_owner=self._lease_owner,
            limit=settings.provider_trigger_generation_batch_size,
            lease_ttl=timedelta(
                seconds=settings.provider_trigger_generation_lease_seconds,
            ),
        )
        if generations:
            self._session.commit()

        processed = 0
        for generation in generations:
            try:
                if self._generation_needs_create(generation):
                    self._process_generation_create(generation)
                elif self._generation_needs_delete(generation):
                    self._process_generation_delete(generation)
                processed += 1
            except Exception:
                logger.exception(
                    "provider-trigger generation operation failed generation_id=%s",
                    generation.generation_id,
                )
                self._mark_generation_operation_retryable(
                    generation,
                    error_code=ReconcileErrorCode.provider_provision_failed.value,
                )
            self._session.commit()
        return {
            "generations_claimed": len(generations),
            "generations_processed": processed,
        }

    def process_health_batch(self) -> dict[str, int]:
        """Run provider health checks for bindings due for polling."""

        bindings = self._dao.list_bindings_for_health(
            limit=settings.provider_trigger_health_batch_size,
            health_interval=timedelta(
                seconds=settings.provider_trigger_health_interval_seconds,
            ),
        )
        checked = 0
        for binding in bindings:
            try:
                self._check_binding_health(binding)
                checked += 1
            except Exception:
                logger.exception(
                    "provider-trigger health check failed binding_id=%s",
                    binding.binding_id,
                )
            self._session.commit()
        return {"bindings_checked": checked}

    def _reconcile_binding(self, binding: EventTriggerBinding) -> None:
        if binding.tombstoned_at is not None:
            self._reconcile_tombstoned_binding(binding)
            return

        desired_state = DesiredTriggerState(binding.desired_trigger_state)
        if desired_state is not DesiredTriggerState.enabled:
            self._reconcile_inactive_binding(binding)
            return

        connection = self._resolve_connection(binding)
        if connection is None:
            self._mark_binding_terminal(
                binding,
                health=BindingRuntimeHealth.needs_attention,
                error_code=ReconcileErrorCode.provider_connection_missing,
            )
            return
        if not self._connection_owned_by_binding(binding, connection):
            self._mark_binding_terminal(
                binding,
                health=BindingRuntimeHealth.needs_attention,
                error_code=ReconcileErrorCode.connection_not_owned,
            )
            return
        if connection.status not in ACTIVE_CONNECTION_STATUSES:
            self._mark_binding_terminal(
                binding,
                health=BindingRuntimeHealth.needs_attention,
                error_code=ReconcileErrorCode.provider_connection_not_active,
            )
            return

        adapter = self._adapter_for_binding(binding)
        if not self._resolve_account_identity(binding, connection, adapter):
            return

        if not self._prerequisites_met(binding, adapter, connection):
            return

        self._drain_stale_generations(binding)
        generation = self._dao.get_matching_generation(binding=binding)
        if generation is None:
            generation = self._dao.create_generation(binding=binding)
        elif (
            generation.create_operation_state
            == GenerationOperationState.succeeded.value
            and generation.external_trigger_id
            and generation.lifecycle_state == GenerationLifecycle.provisioning.value
        ):
            binding = self._dao.get_binding(binding_id=binding.binding_id) or binding
            self._dao.promote_generation(binding=binding, generation=generation)
            binding.consecutive_health_failures = 0
            binding.last_stable_error_code = None
            binding.runtime_health = BindingRuntimeHealth.healthy.value
            self._dao.release_binding_reconcile_lease(binding=binding)
            binding.reconcile_next_retry_at = None
            return

        if (
            generation.lifecycle_state == GenerationLifecycle.active.value
            and binding.active_generation_id == generation.generation_id
            and binding.observed_activation_revision
            == binding.desired_activation_revision
        ):
            binding.runtime_health = BindingRuntimeHealth.healthy.value
            binding.last_stable_error_code = None
            binding.consecutive_health_failures = 0
            self._dao.release_binding_reconcile_lease(binding=binding)
            binding.reconcile_next_retry_at = None
            return

        binding.runtime_health = BindingRuntimeHealth.provisioning.value
        binding.last_stable_error_code = None
        binding.reconcile_attempt_count = 0
        self._dao.schedule_binding_reconcile(
            binding=binding,
            retry_at=datetime.now(timezone.utc) + timedelta(minutes=1),
        )

    def _reconcile_inactive_binding(self, binding: EventTriggerBinding) -> None:
        self._dao.close_acceptance(binding=binding)
        binding.runtime_health = BindingRuntimeHealth.absent.value
        binding.last_stable_error_code = None
        binding.consecutive_health_failures = 0
        for generation in self._dao.list_generations_for_binding(
            binding_id=binding.binding_id,
        ):
            if generation.lifecycle_state in {
                GenerationLifecycle.active.value,
                GenerationLifecycle.draining.value,
                GenerationLifecycle.provisioning.value,
            }:
                self._dao.enqueue_generation_delete(generation=generation)
        self._dao.release_binding_reconcile_lease(binding=binding)
        binding.reconcile_next_retry_at = None

    def _reconcile_tombstoned_binding(self, binding: EventTriggerBinding) -> None:
        self._dao.close_acceptance(binding=binding)
        binding.runtime_health = BindingRuntimeHealth.removing.value
        for generation in self._dao.list_generations_for_binding(
            binding_id=binding.binding_id,
        ):
            if generation.lifecycle_state != GenerationLifecycle.removed.value:
                self._dao.enqueue_generation_delete(generation=generation)
        self._dao.mark_binding_teardown_complete(binding=binding)
        self._dao.release_binding_reconcile_lease(binding=binding)
        binding.reconcile_next_retry_at = datetime.now(timezone.utc) + timedelta(
            minutes=1,
        )

    def _resolve_connection(
        self,
        binding: EventTriggerBinding,
    ) -> IntegrationConnection | None:
        return self._integration_dao.get_connection(binding.connection_id)

    def _connection_owned_by_binding(
        self,
        binding: EventTriggerBinding,
        connection: IntegrationConnection,
    ) -> bool:
        if connection.backend_id != binding.backend_id:
            return False
        if connection.canonical_app_slug != binding.canonical_app_slug:
            return False
        if binding.owner_scope == "assistant":
            return connection.assistant_id == binding.assistant_id
        return True

    def _adapter_for_binding(
        self,
        binding: EventTriggerBinding,
    ) -> TriggerProviderAdapter:
        return self._adapter_resolver(binding.backend_id)

    def _resolve_account_identity(
        self,
        binding: EventTriggerBinding,
        connection: IntegrationConnection,
        adapter: TriggerProviderAdapter,
    ) -> bool:
        provider_connection_id = connection.provider_connection_id
        if not provider_connection_id:
            self._mark_binding_terminal(
                binding,
                health=BindingRuntimeHealth.needs_attention,
                error_code=ReconcileErrorCode.provider_connection_missing,
            )
            return False

        try:
            account = adapter.resolve_account_identity(
                provider_connection_id=provider_connection_id,
            )
        except Exception:
            logger.exception(
                "provider account identity resolution failed binding_id=%s",
                binding.binding_id,
            )
            self._mark_binding_retryable(
                binding,
                error_code=ReconcileErrorCode.provider_health_check_failed.value,
            )
            return False

        resolved_hmac = account.subject_hmac
        if not resolved_hmac and account.subject:
            from orchestra.provider_triggers.composio_trigger_adapter import (
                provider_account_subject_hmac,
            )

            pepper = settings.trigger_event_wrapping_master_key or ""
            if pepper:
                resolved_hmac = provider_account_subject_hmac(
                    account.subject,
                    pepper=pepper,
                )

        if binding.provider_account_subject_hmac:
            if (
                not resolved_hmac
                or resolved_hmac != binding.provider_account_subject_hmac
            ):
                self._mark_binding_terminal(
                    binding,
                    health=BindingRuntimeHealth.needs_attention,
                    error_code=ReconcileErrorCode.account_subject_mismatch,
                )
                return False
        elif resolved_hmac:
            binding.provider_account_subject_hmac = resolved_hmac
            binding.provider_account_display_label = account.display_label

        return True

    def _prerequisites_met(
        self,
        binding: EventTriggerBinding,
        adapter: TriggerProviderAdapter,
        connection: IntegrationConnection,
    ) -> bool:
        from orchestra.provider_triggers.topology import (
            evaluate_provider_trigger_topology,
            topology_reason_to_reconcile_error,
        )

        topology = evaluate_provider_trigger_topology(
            self._session,
            require_worker=False,
        )
        if not topology.available:
            error_code = topology_reason_to_reconcile_error(
                topology.unavailable_reason,
            )
            self._mark_binding_terminal(
                binding,
                health=BindingRuntimeHealth.needs_attention,
                error_code=error_code or ReconcileErrorCode.callback_url_unconfigured,
            )
            return False

        # TODO: Purge/Replace — resolve resource via curated registry +
        # TriggerProviderAdapter; delete github_resource_from_filters call sites
        # outside the adapter (also used from ingress_acceptance).
        # See vault: Provider event trigger contracts#Interim remnants.
        resource_id = github_resource_from_filters(binding.filters_json)
        if not resource_id:
            self._mark_binding_terminal(
                binding,
                health=BindingRuntimeHealth.needs_attention,
                error_code=ReconcileErrorCode.resource_inaccessible,
            )
            return False

        provider_connection_id = connection.provider_connection_id
        if not provider_connection_id:
            self._mark_binding_terminal(
                binding,
                health=BindingRuntimeHealth.needs_attention,
                error_code=ReconcileErrorCode.provider_connection_missing,
            )
            return False

        authorize = getattr(adapter, "authorize_resource", None)
        if callable(authorize) and not authorize(
            provider_connection_id=provider_connection_id,
            resource_id=resource_id,
        ):
            self._mark_binding_terminal(
                binding,
                health=BindingRuntimeHealth.needs_attention,
                error_code=ReconcileErrorCode.resource_inaccessible,
            )
            return False
        return True

    def _drain_stale_generations(self, binding: EventTriggerBinding) -> None:
        for generation in self._dao.list_generations_for_binding(
            binding_id=binding.binding_id,
        ):
            if (
                generation.desired_activation_revision
                != binding.desired_activation_revision
                or generation.acceptance_epoch != binding.acceptance_epoch
            ) and generation.lifecycle_state in {
                GenerationLifecycle.active.value,
                GenerationLifecycle.provisioning.value,
                GenerationLifecycle.draining.value,
            }:
                self._dao.enqueue_generation_delete(generation=generation)

    def _process_generation_create(
        self,
        generation: EventTriggerSubscriptionGeneration,
    ) -> None:
        binding = self._dao.get_binding(binding_id=generation.binding_id)
        if binding is None:
            self._dao.mark_generation_create_failed(
                generation=generation,
                error_code=ReconcileErrorCode.unsupported_event.value,
            )
            return

        if (
            binding.tombstoned_at is not None
            or binding.desired_trigger_state != DesiredTriggerState.enabled.value
            or generation.desired_activation_revision
            != binding.desired_activation_revision
            or generation.acceptance_epoch != binding.acceptance_epoch
            or generation.delete_operation_state
            in {
                GenerationOperationState.pending.value,
                GenerationOperationState.retryable.value,
                GenerationOperationState.claimed.value,
            }
        ):
            self._dao.release_generation_lease(generation=generation)
            generation.create_operation_state = None
            self._session.flush()
            return

        if (
            generation.external_trigger_id
            and generation.create_operation_state
            == GenerationOperationState.succeeded.value
        ):
            self._dao.promote_generation(binding=binding, generation=generation)
            self._dao.release_generation_lease(generation=generation)
            return

        connection = self._resolve_connection(binding)
        if connection is None or not connection.provider_connection_id:
            self._dao.mark_generation_create_failed(
                generation=generation,
                error_code=ReconcileErrorCode.provider_connection_missing.value,
            )
            self._mark_binding_terminal(
                binding,
                health=BindingRuntimeHealth.needs_attention,
                error_code=ReconcileErrorCode.provider_connection_missing,
            )
            return

        adapter = self._adapter_for_binding(binding)
        # TODO: Purge/Replace — resolve resource via curated registry +
        # TriggerProviderAdapter; delete github_resource_from_filters call sites
        # outside the adapter (also used from ingress_acceptance).
        # See vault: Provider event trigger contracts#Interim remnants.
        resource_id = github_resource_from_filters(binding.filters_json) or ""
        callback_base = settings.provider_trigger_callback_base_url or ""
        callback_url = (
            f"{callback_base}/v0/webhooks/integrations/"
            f"{binding.backend_id}/{generation.ingress_key}"
        )
        request = TriggerProvisionRequest(
            connection_id=binding.connection_id,
            provider_connection_id=connection.provider_connection_id,
            provider_user_id=connection.provider_user_id or "",
            event_slug=binding.event_slug,
            schema_version=binding.schema_version,
            canonical_app_slug=binding.canonical_app_slug,
            callback_url=callback_url,
            idempotency_key=generation.provider_create_idempotency_key,
            ingress_key=generation.ingress_key,
            resource_id=resource_id,
            filters=binding.filters_json,
            generation_id=generation.generation_id,
        )
        try:
            result = adapter.provision(request)
        except PermissionError:
            self._dao.mark_generation_create_failed(
                generation=generation,
                error_code=ReconcileErrorCode.provider_permission_denied.value,
            )
            self._mark_binding_terminal(
                binding,
                health=BindingRuntimeHealth.needs_attention,
                error_code=ReconcileErrorCode.provider_permission_denied,
            )
            return
        except Exception:
            logger.exception(
                "provider generation create failed generation_id=%s",
                generation.generation_id,
            )
            self._mark_generation_operation_retryable(
                generation,
                error_code=ReconcileErrorCode.provider_provision_failed.value,
            )
            return

        self._dao.journal_generation_create(
            generation=generation,
            external_trigger_id=result.external_trigger_id,
            signing_secret_ref=result.signing_secret_ref,
            signing_secret_version=result.signing_secret_version,
        )
        binding = self._dao.get_binding(binding_id=binding.binding_id) or binding
        self._dao.promote_generation(binding=binding, generation=generation)
        binding.consecutive_health_failures = 0
        self._dao.release_generation_lease(generation=generation)

    def _process_generation_delete(
        self,
        generation: EventTriggerSubscriptionGeneration,
    ) -> None:
        binding = self._dao.get_binding(binding_id=generation.binding_id)
        if binding is None:
            self._dao.journal_generation_delete(generation=generation)
            return

        if not generation.external_trigger_id:
            self._dao.journal_generation_delete(generation=generation)
            self._dao.clear_active_generation_if_matches(
                binding=binding,
                generation=generation,
            )
            self._dao.mark_binding_teardown_complete(binding=binding)
            return

        connection = self._resolve_connection(binding)
        adapter = self._adapter_for_binding(binding)
        request = TriggerDeleteRequest(
            external_trigger_id=generation.external_trigger_id,
            idempotency_key=generation.provider_delete_idempotency_key or "",
            provider_connection_id=(
                connection.provider_connection_id if connection else None
            ),
            provider_user_id=connection.provider_user_id if connection else None,
        )
        try:
            adapter.delete(request)
        except Exception:
            logger.exception(
                "provider generation delete failed generation_id=%s",
                generation.generation_id,
            )
            self._mark_generation_operation_retryable(
                generation,
                error_code=ReconcileErrorCode.provider_delete_failed.value,
                delete=True,
            )
            return

        self._dao.journal_generation_delete(generation=generation)
        self._dao.clear_active_generation_if_matches(
            binding=binding,
            generation=generation,
        )
        self._dao.mark_binding_teardown_complete(binding=binding)

    def _check_binding_health(self, binding: EventTriggerBinding) -> None:
        now = datetime.now(timezone.utc)
        binding.last_health_check_at = now

        connection = self._resolve_connection(binding)
        if connection is None or not connection.provider_connection_id:
            self._record_health_failure(
                binding,
                error_code=ReconcileErrorCode.provider_connection_missing.value,
            )
            return

        generation = None
        if binding.active_generation_id:
            for item in self._dao.list_generations_for_binding(
                binding_id=binding.binding_id,
            ):
                if item.generation_id == binding.active_generation_id:
                    generation = item
                    break

        adapter = self._adapter_for_binding(binding)
        try:
            health = adapter.health(
                external_trigger_id=(
                    generation.external_trigger_id if generation else None
                ),
                provider_connection_id=connection.provider_connection_id,
            )
        except Exception:
            logger.exception(
                "provider-trigger health check failed binding_id=%s",
                binding.binding_id,
            )
            self._record_health_failure(
                binding,
                error_code=ReconcileErrorCode.provider_health_check_failed.value,
            )
            return

        if health.status == "ok":
            binding.consecutive_health_failures = 0
            binding.last_stable_error_code = None
            if binding.runtime_health == BindingRuntimeHealth.recovering.value:
                binding.runtime_health = BindingRuntimeHealth.healthy.value
            return

        error_code = (
            health.error_code or ReconcileErrorCode.provider_health_check_failed.value
        )
        self._record_health_failure(binding, error_code=error_code)

    def _record_health_failure(
        self,
        binding: EventTriggerBinding,
        *,
        error_code: str,
    ) -> None:
        binding.consecutive_health_failures += 1
        binding.last_stable_error_code = error_code
        threshold = settings.provider_trigger_health_failure_threshold
        if binding.consecutive_health_failures >= threshold:
            binding.runtime_health = BindingRuntimeHealth.needs_attention.value
            self._dao.close_acceptance(binding=binding)
            binding.reconcile_next_retry_at = datetime.now(timezone.utc)
            return

        binding.runtime_health = BindingRuntimeHealth.recovering.value
        binding.reconcile_next_retry_at = datetime.now(timezone.utc) + timedelta(
            seconds=settings.provider_trigger_health_interval_seconds,
        )

    def _generation_needs_create(
        self,
        generation: EventTriggerSubscriptionGeneration,
    ) -> bool:
        return generation.create_operation_state in {
            GenerationOperationState.pending.value,
            GenerationOperationState.retryable.value,
            GenerationOperationState.claimed.value,
        }

    def _generation_needs_delete(
        self,
        generation: EventTriggerSubscriptionGeneration,
    ) -> bool:
        return generation.delete_operation_state in {
            GenerationOperationState.pending.value,
            GenerationOperationState.retryable.value,
            GenerationOperationState.claimed.value,
        }

    def _next_retry_at(self, attempt_count: int) -> datetime:
        delay_minutes = min(
            MAX_RETRY_DELAY_MINUTES,
            BASE_RETRY_DELAY_MINUTES * (2 ** max(0, attempt_count - 1)),
        )
        return datetime.now(timezone.utc) + timedelta(minutes=delay_minutes)

    def _mark_binding_terminal(
        self,
        binding: EventTriggerBinding,
        *,
        health: BindingRuntimeHealth,
        error_code: ReconcileErrorCode | str,
    ) -> None:
        code = (
            error_code.value
            if isinstance(error_code, ReconcileErrorCode)
            else str(error_code)
        )
        binding.runtime_health = health.value
        binding.last_stable_error_code = code
        self._dao.close_acceptance(binding=binding)
        self._dao.release_binding_reconcile_lease(binding=binding)
        binding.reconcile_next_retry_at = None

    def _mark_binding_retryable(
        self,
        binding: EventTriggerBinding,
        *,
        error_code: str,
    ) -> None:
        binding.reconcile_attempt_count += 1
        binding.last_stable_error_code = error_code
        if (
            binding.reconcile_attempt_count
            >= settings.provider_trigger_max_reconcile_attempts
        ):
            binding.runtime_health = BindingRuntimeHealth.needs_attention.value
            self._dao.close_acceptance(binding=binding)
            binding.reconcile_next_retry_at = None
        else:
            binding.runtime_health = BindingRuntimeHealth.recovering.value
            self._dao.schedule_binding_reconcile(
                binding=binding,
                retry_at=self._next_retry_at(binding.reconcile_attempt_count),
            )
        self._dao.release_binding_reconcile_lease(binding=binding)

    def _mark_generation_operation_retryable(
        self,
        generation: EventTriggerSubscriptionGeneration,
        *,
        error_code: str,
        delete: bool = False,
    ) -> None:
        retry_at = self._next_retry_at(generation.attempt_count)
        if (
            generation.attempt_count
            >= settings.provider_trigger_max_generation_attempts
        ):
            if delete:
                self._dao.mark_generation_delete_failed(
                    generation=generation,
                    error_code=error_code,
                )
            else:
                self._dao.mark_generation_create_failed(
                    generation=generation,
                    error_code=error_code,
                )
            binding = self._dao.get_binding(binding_id=generation.binding_id)
            if binding is not None:
                self._mark_binding_terminal(
                    binding,
                    health=BindingRuntimeHealth.needs_attention,
                    error_code=error_code,
                )
            return

        if delete:
            self._dao.mark_generation_delete_retryable(
                generation=generation,
                error_code=error_code,
                retry_at=retry_at,
            )
        else:
            self._dao.mark_generation_create_retryable(
                generation=generation,
                error_code=error_code,
                retry_at=retry_at,
            )
