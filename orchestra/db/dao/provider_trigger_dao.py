"""Data access for provider-event trigger runtime state."""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.models.provider_trigger_models import (
    EventTriggerBinding,
    EventTriggerSubscriptionGeneration,
    ProviderEventDispatch,
    ProviderEventReceipt,
)
from orchestra.provider_triggers.activation_revision import (
    compute_provider_event_activation_revision,
    normalize_provider_event_filters,
)
from orchestra.provider_triggers.runtime_types import (
    BindingRuntimeHealth,
    DesiredTriggerState,
    DispatchProcessingState,
    GenerationLifecycle,
    ReceiptProcessingState,
)
from orchestra.provider_triggers.task_trigger import ProviderEventTrigger


class ProviderTriggerDAO:
    """Reads and writes provider-event binding runtime state."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get_binding(
        self,
        *,
        binding_id: str,
        for_update: bool = False,
    ) -> EventTriggerBinding | None:
        query = select(EventTriggerBinding).where(
            EventTriggerBinding.binding_id == binding_id,
        )
        if for_update:
            query = query.with_for_update()
        return self.session.execute(query).scalar_one_or_none()

    def get_binding_for_task(
        self,
        *,
        project_id: int,
        source_task_log_id: int,
        for_update: bool = False,
    ) -> EventTriggerBinding | None:
        query = select(EventTriggerBinding).where(
            EventTriggerBinding.project_id == project_id,
            EventTriggerBinding.source_task_log_id == source_task_log_id,
        )
        if for_update:
            query = query.with_for_update()
        return self.session.execute(query).scalar_one_or_none()

    def create_binding(
        self,
        *,
        binding_id: str,
        project_id: int,
        tasks_context_id: int,
        source_task_log_id: int,
        task_id: int,
        assistant_id: int,
        task_revision: int,
        trigger: ProviderEventTrigger,
        execution_mode: str,
        entrypoint: int | None,
        task_enabled: bool = True,
    ) -> EventTriggerBinding:
        """Insert one derived binding for a new provider-event task."""

        desired_state = DesiredTriggerState(trigger.state)
        desired_revision = compute_provider_event_activation_revision(
            trigger=trigger,
            binding_id=binding_id,
            execution_mode=execution_mode,
            entrypoint=entrypoint,
        )
        runtime_health = BindingRuntimeHealth.absent
        if task_enabled and desired_state is DesiredTriggerState.enabled:
            runtime_health = BindingRuntimeHealth.provisioning
        binding = EventTriggerBinding(
            binding_id=binding_id,
            project_id=project_id,
            tasks_context_id=tasks_context_id,
            source_task_log_id=source_task_log_id,
            task_id=task_id,
            assistant_id=assistant_id,
            task_revision=task_revision,
            desired_trigger_state=desired_state.value,
            desired_activation_revision=desired_revision,
            acceptance_epoch=1,
            connection_id=trigger.connection_id,
            backend_id=trigger.backend_id,
            canonical_app_slug=trigger.canonical_app_slug,
            event_slug=trigger.event_slug,
            schema_version=trigger.schema_version,
            filters_json=normalize_provider_event_filters(
                [item.model_dump() for item in trigger.filters],
            ),
            execution_mode=execution_mode,
            entrypoint=entrypoint,
            runtime_health=runtime_health.value,
            local_acceptance_open=False,
        )
        self.session.add(binding)
        self.session.flush()
        return binding

    def sync_desired_state(
        self,
        *,
        binding: EventTriggerBinding,
        task_revision: int,
        trigger: ProviderEventTrigger,
        execution_mode: str,
        entrypoint: int | None,
        bump_acceptance_epoch: bool,
        task_enabled: bool = True,
    ) -> EventTriggerBinding:
        """Mirror authored task intent onto the binding."""

        desired_state = DesiredTriggerState(trigger.state)
        binding.task_revision = task_revision
        binding.desired_trigger_state = desired_state.value
        binding.desired_activation_revision = (
            compute_provider_event_activation_revision(
                trigger=trigger,
                binding_id=binding.binding_id,
                execution_mode=execution_mode,
                entrypoint=entrypoint,
                provider_account_subject_hmac=binding.provider_account_subject_hmac,
            )
        )
        binding.connection_id = trigger.connection_id
        binding.backend_id = trigger.backend_id
        binding.canonical_app_slug = trigger.canonical_app_slug
        binding.event_slug = trigger.event_slug
        binding.schema_version = trigger.schema_version
        binding.filters_json = normalize_provider_event_filters(
            [item.model_dump() for item in trigger.filters],
        )
        binding.execution_mode = execution_mode
        binding.entrypoint = entrypoint
        if bump_acceptance_epoch:
            binding.acceptance_epoch += 1
        binding.local_acceptance_open = False
        if binding.tombstoned_at is None:
            if not task_enabled:
                binding.runtime_health = BindingRuntimeHealth.absent.value
            elif desired_state is DesiredTriggerState.enabled:
                binding.runtime_health = BindingRuntimeHealth.provisioning.value
                binding.reconcile_next_retry_at = datetime.now(timezone.utc)
            elif desired_state is DesiredTriggerState.paused:
                binding.runtime_health = BindingRuntimeHealth.absent.value
            else:
                binding.runtime_health = BindingRuntimeHealth.absent.value
        self.session.flush()
        return binding

    def tombstone_binding(self, *, binding: EventTriggerBinding) -> EventTriggerBinding:
        """Close acceptance and retain the binding for ingress teardown."""

        binding.acceptance_epoch += 1
        binding.local_acceptance_open = False
        binding.runtime_health = BindingRuntimeHealth.removing.value
        binding.coverage_ended_at = datetime.now(timezone.utc)
        binding.tombstoned_at = datetime.now(timezone.utc)
        self.session.flush()
        return binding

    def request_reconcile(self, *, binding: EventTriggerBinding) -> EventTriggerBinding:
        """Request immediate reconciliation for the current desired revision."""

        binding.reconcile_next_retry_at = datetime.now(timezone.utc)
        binding.reconcile_attempt_count += 1
        if (
            binding.tombstoned_at is None
            and binding.desired_trigger_state == DesiredTriggerState.enabled.value
        ):
            binding.runtime_health = BindingRuntimeHealth.provisioning.value
        self.session.flush()
        return binding

    def create_generation(
        self,
        *,
        binding: EventTriggerBinding,
        generation_id: str | None = None,
    ) -> EventTriggerSubscriptionGeneration:
        """Create one provisioning generation for a binding."""

        resolved_generation_id = generation_id or f"gen-{uuid.uuid4().hex[:12]}"
        generation = EventTriggerSubscriptionGeneration(
            generation_id=resolved_generation_id,
            binding_id=binding.binding_id,
            desired_activation_revision=binding.desired_activation_revision,
            acceptance_epoch=binding.acceptance_epoch,
            provider_create_idempotency_key=f"create-{resolved_generation_id}",
            ingress_key=secrets.token_urlsafe(24),
            lifecycle_state=GenerationLifecycle.provisioning.value,
        )
        self.session.add(generation)
        self.session.flush()
        return generation

    def promote_generation(
        self,
        *,
        binding: EventTriggerBinding,
        generation: EventTriggerSubscriptionGeneration,
    ) -> EventTriggerBinding:
        """Promote one generation and open local acceptance."""

        if binding.tombstoned_at is not None:
            raise ValueError("cannot_promote_tombstoned_binding")

        if (
            binding.active_generation_id
            and binding.active_generation_id != generation.generation_id
        ):
            previous = self.session.execute(
                select(EventTriggerSubscriptionGeneration).where(
                    EventTriggerSubscriptionGeneration.generation_id
                    == binding.active_generation_id,
                ),
            ).scalar_one_or_none()
            if previous is not None:
                previous.lifecycle_state = GenerationLifecycle.draining.value
                previous.deactivated_at = datetime.now(timezone.utc)

        generation.lifecycle_state = GenerationLifecycle.active.value
        generation.activated_at = datetime.now(timezone.utc)
        binding.active_generation_id = generation.generation_id
        binding.observed_activation_revision = generation.desired_activation_revision
        binding.local_acceptance_open = True
        binding.runtime_health = BindingRuntimeHealth.healthy.value
        binding.coverage_started_at = datetime.now(timezone.utc)
        binding.coverage_ended_at = None
        self.session.flush()
        return binding

    def adopt_receipt(
        self,
        *,
        binding: EventTriggerBinding,
        generation: EventTriggerSubscriptionGeneration,
        provider_event_identity_hmac: str,
        receipt_id: str | None = None,
        acceptance_authorization_json: dict[str, Any] | None = None,
    ) -> ProviderEventReceipt:
        """Insert or adopt one receipt under the binding identity constraint."""

        resolved_receipt_id = receipt_id or f"receipt-{uuid.uuid4().hex[:12]}"
        existing = self.session.execute(
            select(ProviderEventReceipt).where(
                ProviderEventReceipt.binding_id == binding.binding_id,
                ProviderEventReceipt.provider_event_identity_hmac
                == provider_event_identity_hmac,
            ),
        ).scalar_one_or_none()
        if existing is not None:
            return existing

        receipt = ProviderEventReceipt(
            receipt_id=resolved_receipt_id,
            binding_id=binding.binding_id,
            generation_id=generation.generation_id,
            provider_event_identity_hmac=provider_event_identity_hmac,
            accepted_activation_revision=generation.desired_activation_revision,
            acceptance_epoch=generation.acceptance_epoch,
            schema_version=binding.schema_version,
            processing_state=ReceiptProcessingState.accepted.value,
            acceptance_authorization_json=acceptance_authorization_json or {},
            classification_reason="matched",
        )
        self.session.add(receipt)
        self.session.flush()
        binding.last_accepted_event_at = datetime.now(timezone.utc)
        self.session.flush()
        return receipt

    def adopt_dispatch(
        self,
        *,
        receipt: ProviderEventReceipt,
        binding: EventTriggerBinding,
        operation_id: str | None = None,
        run_id: int | None = None,
        run_key: str | None = None,
        audience: str = "unity",
    ) -> ProviderEventDispatch:
        """Insert or adopt one dispatch operation for a receipt."""

        existing = self.session.execute(
            select(ProviderEventDispatch).where(
                ProviderEventDispatch.receipt_id == receipt.receipt_id,
            ),
        ).scalar_one_or_none()
        if existing is not None:
            return existing

        resolved_operation_id = operation_id or f"op-{uuid.uuid4().hex[:12]}"
        resolved_run_id = run_id if run_id is not None else 0
        resolved_run_key = run_key or f"run-{resolved_operation_id}"
        dispatch = ProviderEventDispatch(
            operation_id=resolved_operation_id,
            receipt_id=receipt.receipt_id,
            binding_id=binding.binding_id,
            assistant_id=binding.assistant_id,
            task_id=binding.task_id,
            run_id=resolved_run_id,
            run_key=resolved_run_key,
            dispatch_mode=binding.execution_mode,
            accepted_activation_revision=receipt.accepted_activation_revision,
            event_context_ref=receipt.event_context_ref,
            audience=audience,
            processing_state=DispatchProcessingState.pending.value,
        )
        self.session.add(dispatch)
        self.session.flush()

        receipt.dispatch_operation_id = dispatch.operation_id
        receipt.run_key = dispatch.run_key
        receipt.run_id = dispatch.run_id
        receipt.dispatch_mode = dispatch.dispatch_mode
        receipt.processing_state = ReceiptProcessingState.dispatch_pending.value
        self.session.flush()
        return dispatch
