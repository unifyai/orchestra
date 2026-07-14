"""Data access for provider-event trigger runtime state."""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from orchestra.db.models.provider_trigger_models import (
    EventTriggerBinding,
    EventTriggerSubscriptionGeneration,
    ProviderEventBlob,
    ProviderEventBlobAudit,
    ProviderEventBlobDeletion,
    ProviderEventDispatch,
    ProviderEventReceipt,
    ProviderTriggerWorkerHeartbeat,
)
from orchestra.provider_triggers.activation_revision import (
    compute_provider_event_activation_revision,
)
from orchestra.provider_triggers.private_event_storage import EncryptedEventObject
from orchestra.provider_triggers.runtime_types import (
    BindingRuntimeHealth,
    BlobAuditAction,
    BlobCommitState,
    BlobDeletionState,
    DesiredTriggerState,
    DispatchErrorCode,
    DispatchProcessingState,
    GenerationLifecycle,
    GenerationOperationState,
    ReceiptProcessingState,
)
from orchestra.provider_triggers.task_trigger import ProviderEventTrigger
from orchestra.provider_triggers.trigger_registry import curated_provider_event_filters
from orchestra.settings import settings

STALE_RECONCILE_PROCESSING = timedelta(minutes=5)
BASE_DISPATCH_RETRY_DELAY_MINUTES = 5
MAX_DISPATCH_RETRY_DELAY_MINUTES = 60


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
        reconcile_next_retry_at = None
        if task_enabled and desired_state is DesiredTriggerState.enabled:
            runtime_health = BindingRuntimeHealth.provisioning
            reconcile_next_retry_at = datetime.now(timezone.utc)
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
            filters_json=curated_provider_event_filters(
                trigger.event_slug,
                trigger.schema_version,
                [item.model_dump() for item in trigger.filters],
            ),
            execution_mode=execution_mode,
            entrypoint=entrypoint,
            runtime_health=runtime_health.value,
            local_acceptance_open=False,
            reconcile_next_retry_at=reconcile_next_retry_at,
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
        binding.filters_json = curated_provider_event_filters(
            trigger.event_slug,
            trigger.schema_version,
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
        binding.reconcile_next_retry_at = datetime.now(timezone.utc)
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
            create_operation_state=GenerationOperationState.pending.value,
            next_retry_at=datetime.now(timezone.utc),
        )
        self.session.add(generation)
        self.session.flush()
        return generation

    def get_generation(
        self,
        *,
        generation_id: str,
        for_update: bool = False,
    ) -> EventTriggerSubscriptionGeneration | None:
        """Return one subscription generation by id."""

        query = select(EventTriggerSubscriptionGeneration).where(
            EventTriggerSubscriptionGeneration.generation_id == generation_id,
        )
        if for_update:
            query = query.with_for_update()
        return self.session.execute(query).scalar_one_or_none()

    def get_generation_by_ingress_key(
        self,
        *,
        backend_id: str,
        ingress_key: str,
    ) -> tuple[EventTriggerSubscriptionGeneration, EventTriggerBinding] | None:
        """Resolve one generation and its binding from the public ingress key."""

        row = self.session.execute(
            select(EventTriggerSubscriptionGeneration, EventTriggerBinding)
            .join(
                EventTriggerBinding,
                EventTriggerBinding.binding_id
                == EventTriggerSubscriptionGeneration.binding_id,
            )
            .where(
                EventTriggerSubscriptionGeneration.ingress_key == ingress_key,
                EventTriggerBinding.backend_id == backend_id,
            ),
        ).one_or_none()
        if row is None:
            return None
        return row[0], row[1]

    def promote_generation(
        self,
        *,
        binding: EventTriggerBinding,
        generation: EventTriggerSubscriptionGeneration,
    ) -> EventTriggerBinding:
        """Promote one generation and open local acceptance."""

        if binding.tombstoned_at is not None:
            raise ValueError("cannot_promote_tombstoned_binding")

        if not settings.provider_event_storage_configured:
            binding.runtime_health = BindingRuntimeHealth.needs_attention.value
            binding.local_acceptance_open = False
            binding.last_stable_error_code = "event_storage_unconfigured"
            generation.lifecycle_state = GenerationLifecycle.failed.value
            self.session.flush()
            return binding

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

    def get_receipt_by_identity(
        self,
        *,
        binding_id: str,
        provider_event_identity_hmac: str,
    ) -> ProviderEventReceipt | None:
        """Return the durable receipt for one binding/event identity when present."""

        return self.session.execute(
            select(ProviderEventReceipt).where(
                ProviderEventReceipt.binding_id == binding_id,
                ProviderEventReceipt.provider_event_identity_hmac
                == provider_event_identity_hmac,
            ),
        ).scalar_one_or_none()

    def adopt_receipt(
        self,
        *,
        binding: EventTriggerBinding,
        generation: EventTriggerSubscriptionGeneration,
        provider_event_identity_hmac: str,
        receipt_id: str | None = None,
        acceptance_authorization_json: dict[str, Any] | None = None,
        processing_state: str = ReceiptProcessingState.accepted.value,
        classification_reason: str = "matched",
        stable_envelope_json: dict[str, Any] | None = None,
        curated_projection_json: dict[str, Any] | None = None,
    ) -> ProviderEventReceipt:
        """Insert or adopt one receipt under the binding identity constraint."""

        resolved_receipt_id = receipt_id or f"receipt-{uuid.uuid4().hex[:12]}"
        existing = self.get_receipt_by_identity(
            binding_id=binding.binding_id,
            provider_event_identity_hmac=provider_event_identity_hmac,
        )
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
            processing_state=processing_state,
            acceptance_authorization_json=acceptance_authorization_json or {},
            classification_reason=classification_reason,
            stable_envelope_json=stable_envelope_json,
            curated_projection_json=curated_projection_json,
        )
        self.session.add(receipt)
        self.session.flush()
        if processing_state != ReceiptProcessingState.ignored.value:
            binding.last_accepted_event_at = datetime.now(timezone.utc)
            self.session.flush()
        return receipt

    def adopt_dispatch(
        self,
        *,
        receipt: ProviderEventReceipt,
        binding: EventTriggerBinding,
        run_id: int,
        run_key: str,
        audience: str,
        operation_id: str | None = None,
    ) -> ProviderEventDispatch:
        """Insert or adopt one dispatch operation for a receipt."""

        if run_id is None or not run_key:
            raise ValueError("dispatch_requires_run_identity")
        if not audience:
            raise ValueError("dispatch_requires_audience")

        existing = self.session.execute(
            select(ProviderEventDispatch).where(
                ProviderEventDispatch.receipt_id == receipt.receipt_id,
            ),
        ).scalar_one_or_none()
        if existing is not None:
            return existing

        resolved_operation_id = operation_id or f"op-{uuid.uuid4().hex[:12]}"
        dispatch = ProviderEventDispatch(
            operation_id=resolved_operation_id,
            receipt_id=receipt.receipt_id,
            binding_id=binding.binding_id,
            assistant_id=binding.assistant_id,
            task_id=binding.task_id,
            run_id=run_id,
            run_key=run_key,
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

    def get_dispatch_by_operation_id(
        self,
        *,
        operation_id: str,
        for_update: bool = False,
    ) -> ProviderEventDispatch | None:
        """Return one dispatch operation by its immutable operation id."""

        query = select(ProviderEventDispatch).where(
            ProviderEventDispatch.operation_id == operation_id,
        )
        if for_update:
            query = query.with_for_update()
        return self.session.execute(query).scalar_one_or_none()

    def get_receipt_by_id(
        self,
        *,
        receipt_id: str,
    ) -> ProviderEventReceipt | None:
        """Return one durable receipt by receipt id."""

        return self.session.execute(
            select(ProviderEventReceipt).where(
                ProviderEventReceipt.receipt_id == receipt_id,
            ),
        ).scalar_one_or_none()

    def _claimable_dispatch_filter(self, now: datetime):
        """Dispatches due for delivery with an available or expired lease."""

        state_due = ProviderEventDispatch.processing_state.in_(
            [
                DispatchProcessingState.pending.value,
                DispatchProcessingState.retryable.value,
                DispatchProcessingState.claimed.value,
            ],
        )
        retry_due = or_(
            ProviderEventDispatch.next_retry_at.is_(None),
            ProviderEventDispatch.next_retry_at <= now,
        )
        lease_available = or_(
            ProviderEventDispatch.lease_expires_at.is_(None),
            ProviderEventDispatch.lease_expires_at <= now,
        )
        return and_(state_due, retry_due, lease_available)

    def claim_dispatches_for_delivery(
        self,
        *,
        lease_owner: str,
        limit: int,
        lease_ttl: timedelta,
    ) -> list[ProviderEventDispatch]:
        """Claim a batch of dispatch operations for outbound delivery."""

        now = datetime.now(timezone.utc)
        claimable = (
            select(ProviderEventDispatch)
            .where(self._claimable_dispatch_filter(now))
            .order_by(ProviderEventDispatch.next_retry_at.asc().nullsfirst())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        dispatches = list(self.session.execute(claimable).scalars())
        lease_expires_at = now + lease_ttl
        for dispatch in dispatches:
            dispatch.processing_state = DispatchProcessingState.claimed.value
            dispatch.lease_owner = lease_owner
            dispatch.lease_expires_at = lease_expires_at
            dispatch.attempt_count += 1
        if dispatches:
            self.session.flush()
        return dispatches

    def _convergeable_dispatch_filter(self, now: datetime):
        """Dispatches due for downstream status convergence polling."""

        state_due = ProviderEventDispatch.processing_state.in_(
            [
                DispatchProcessingState.delivered.value,
                DispatchProcessingState.started.value,
            ],
        )
        retry_due = or_(
            ProviderEventDispatch.next_retry_at.is_(None),
            ProviderEventDispatch.next_retry_at <= now,
        )
        return and_(state_due, retry_due)

    def list_dispatches_for_status_convergence(
        self,
        *,
        limit: int,
    ) -> list[ProviderEventDispatch]:
        """Return dispatches due for downstream status convergence."""

        now = datetime.now(timezone.utc)
        rows = self.session.execute(
            select(ProviderEventDispatch)
            .where(self._convergeable_dispatch_filter(now))
            .order_by(ProviderEventDispatch.next_retry_at.asc().nullsfirst())
            .limit(limit),
        ).scalars()
        return list(rows)

    def list_dispatch_backlog(
        self,
        *,
        limit: int,
    ) -> list[ProviderEventDispatch]:
        """Return the oldest non-terminal dispatch operations."""

        terminal_states = {
            DispatchProcessingState.succeeded.value,
            DispatchProcessingState.failed.value,
        }
        rows = self.session.execute(
            select(ProviderEventDispatch)
            .where(
                ProviderEventDispatch.processing_state.notin_(terminal_states),
            )
            .order_by(ProviderEventDispatch.created_at.asc())
            .limit(limit),
        ).scalars()
        return list(rows)

    def release_dispatch_lease(
        self,
        *,
        dispatch: ProviderEventDispatch,
    ) -> None:
        """Clear the delivery lease on one dispatch operation."""

        dispatch.lease_owner = None
        dispatch.lease_expires_at = None
        self.session.flush()

    def _dispatch_retry_at(self, attempt_count: int) -> datetime:
        delay_minutes = min(
            BASE_DISPATCH_RETRY_DELAY_MINUTES * (2 ** max(0, attempt_count - 1)),
            MAX_DISPATCH_RETRY_DELAY_MINUTES,
        )
        return datetime.now(timezone.utc) + timedelta(minutes=delay_minutes)

    def _sync_receipt_processing_state(
        self,
        *,
        dispatch: ProviderEventDispatch,
        receipt_state: str,
    ) -> None:
        receipt = self.get_receipt_by_id(receipt_id=dispatch.receipt_id)
        if receipt is None:
            return
        receipt.processing_state = receipt_state
        now = datetime.now(timezone.utc)
        receipt.last_attempt_at = now
        if receipt.first_attempt_at is None:
            receipt.first_attempt_at = now
        self.session.flush()

    def record_downstream_adoption(
        self,
        *,
        dispatch: ProviderEventDispatch,
        adoption_status: str,
        adoption_ref: str | None = None,
    ) -> ProviderEventDispatch:
        """Persist the latest downstream adoption observation."""

        now = datetime.now(timezone.utc)
        dispatch.downstream_adoption_status = adoption_status
        dispatch.downstream_adoption_ref = adoption_ref
        dispatch.downstream_status_at = now
        self.session.flush()
        return dispatch

    def mark_dispatch_delivered(
        self,
        *,
        dispatch: ProviderEventDispatch,
        adoption_status: str,
        adoption_ref: str | None = None,
        poll_after_seconds: int | None = None,
    ) -> ProviderEventDispatch:
        """Record successful handoff to the execution rail."""

        now = datetime.now(timezone.utc)
        dispatch.processing_state = DispatchProcessingState.delivered.value
        dispatch.delivered_at = now
        dispatch.terminal_error_code = None
        poll_seconds = poll_after_seconds or (
            settings.provider_trigger_dispatch_poll_interval_seconds
        )
        dispatch.next_retry_at = now + timedelta(seconds=poll_seconds)
        self.record_downstream_adoption(
            dispatch=dispatch,
            adoption_status=adoption_status,
            adoption_ref=adoption_ref,
        )
        self._sync_receipt_processing_state(
            dispatch=dispatch,
            receipt_state=ReceiptProcessingState.dispatched.value,
        )
        self.release_dispatch_lease(dispatch=dispatch)
        return dispatch

    def mark_dispatch_started(
        self,
        *,
        dispatch: ProviderEventDispatch,
        adoption_status: str,
        adoption_ref: str | None = None,
        poll_after_seconds: int | None = None,
    ) -> ProviderEventDispatch:
        """Record that downstream execution has started."""

        now = datetime.now(timezone.utc)
        dispatch.processing_state = DispatchProcessingState.started.value
        if dispatch.started_at is None:
            dispatch.started_at = now
        dispatch.terminal_error_code = None
        poll_seconds = poll_after_seconds or (
            settings.provider_trigger_dispatch_poll_interval_seconds
        )
        dispatch.next_retry_at = now + timedelta(seconds=poll_seconds)
        self.record_downstream_adoption(
            dispatch=dispatch,
            adoption_status=adoption_status,
            adoption_ref=adoption_ref,
        )
        self._sync_receipt_processing_state(
            dispatch=dispatch,
            receipt_state=ReceiptProcessingState.started.value,
        )
        self.release_dispatch_lease(dispatch=dispatch)
        return dispatch

    def mark_dispatch_succeeded(
        self,
        *,
        dispatch: ProviderEventDispatch,
        adoption_status: str,
        adoption_ref: str | None = None,
    ) -> ProviderEventDispatch:
        """Record terminal success for one dispatch operation."""

        now = datetime.now(timezone.utc)
        dispatch.processing_state = DispatchProcessingState.succeeded.value
        dispatch.terminal_at = now
        dispatch.next_retry_at = None
        self.record_downstream_adoption(
            dispatch=dispatch,
            adoption_status=adoption_status,
            adoption_ref=adoption_ref,
        )
        self._sync_receipt_processing_state(
            dispatch=dispatch,
            receipt_state=ReceiptProcessingState.succeeded.value,
        )
        receipt = self.get_receipt_by_id(receipt_id=dispatch.receipt_id)
        if receipt is not None:
            receipt.terminal_at = now
            receipt.terminal_reason = adoption_status
            self.session.flush()
        self.release_dispatch_lease(dispatch=dispatch)
        return dispatch

    def mark_dispatch_failed(
        self,
        *,
        dispatch: ProviderEventDispatch,
        error_code: str,
        adoption_status: str | None = None,
        adoption_ref: str | None = None,
    ) -> ProviderEventDispatch:
        """Record terminal failure for one dispatch operation."""

        now = datetime.now(timezone.utc)
        dispatch.processing_state = DispatchProcessingState.failed.value
        dispatch.terminal_error_code = error_code
        dispatch.terminal_at = now
        dispatch.next_retry_at = None
        if adoption_status is not None:
            self.record_downstream_adoption(
                dispatch=dispatch,
                adoption_status=adoption_status,
                adoption_ref=adoption_ref,
            )
        self._sync_receipt_processing_state(
            dispatch=dispatch,
            receipt_state=ReceiptProcessingState.failed.value,
        )
        receipt = self.get_receipt_by_id(receipt_id=dispatch.receipt_id)
        if receipt is not None:
            receipt.terminal_at = now
            receipt.terminal_reason = error_code
            self.session.flush()
        self.release_dispatch_lease(dispatch=dispatch)
        return dispatch

    def mark_dispatch_retryable(
        self,
        *,
        dispatch: ProviderEventDispatch,
        error_code: str,
    ) -> ProviderEventDispatch:
        """Schedule a retryable dispatch delivery failure."""

        dispatch.processing_state = DispatchProcessingState.retryable.value
        dispatch.terminal_error_code = error_code
        if dispatch.attempt_count >= settings.provider_trigger_dispatch_max_attempts:
            return self.mark_dispatch_failed(
                dispatch=dispatch,
                error_code=DispatchErrorCode.dispatch_max_attempts_exceeded.value,
            )
        dispatch.next_retry_at = self._dispatch_retry_at(dispatch.attempt_count)
        self._sync_receipt_processing_state(
            dispatch=dispatch,
            receipt_state=ReceiptProcessingState.retryable.value,
        )
        self.release_dispatch_lease(dispatch=dispatch)
        return dispatch

    def requeue_dispatch_for_repair(
        self,
        *,
        dispatch: ProviderEventDispatch,
    ) -> ProviderEventDispatch:
        """Requeue one non-terminal dispatch operation for worker delivery."""

        if dispatch.processing_state in {
            DispatchProcessingState.succeeded.value,
            DispatchProcessingState.failed.value,
            DispatchProcessingState.delivered.value,
            DispatchProcessingState.started.value,
        }:
            raise ValueError("dispatch_not_repairable")
        dispatch.processing_state = DispatchProcessingState.pending.value
        dispatch.next_retry_at = datetime.now(timezone.utc)
        dispatch.lease_owner = None
        dispatch.lease_expires_at = None
        dispatch.terminal_error_code = None
        self.session.flush()
        return dispatch

    def create_uncommitted_blob(
        self,
        *,
        blob_id: str,
        binding_id: str,
        receipt_id: str,
        encrypted: EncryptedEventObject,
    ) -> ProviderEventBlob:
        """Insert one uncommitted private event blob metadata row."""

        blob = ProviderEventBlob(
            blob_id=blob_id,
            namespace_key=encrypted.namespace_key,
            binding_id=binding_id,
            receipt_id=receipt_id,
            algorithm=encrypted.algorithm,
            wrap_algorithm=encrypted.wrap_algorithm,
            wrapping_key_version=encrypted.wrapping_key_version,
            wrapped_data_key=encrypted.wrapped_data_key,
            integrity_hash=encrypted.integrity_hash,
            size_bytes=encrypted.size_bytes,
            content_type=encrypted.content_type,
            commit_state=BlobCommitState.uncommitted.value,
        )
        self.session.add(blob)
        self.session.flush()
        return blob

    def attach_event_context(
        self,
        *,
        receipt: ProviderEventReceipt,
        blob: ProviderEventBlob,
    ) -> ProviderEventReceipt:
        """Commit one blob and attach thin refs to the receipt."""

        if blob.commit_state != BlobCommitState.uncommitted.value:
            raise ValueError("blob_not_uncommitted")
        if (
            blob.binding_id != receipt.binding_id
            or blob.receipt_id != receipt.receipt_id
        ):
            raise ValueError("blob_receipt_mismatch")

        now = datetime.now(timezone.utc)
        blob.commit_state = BlobCommitState.committed.value
        blob.committed_at = now
        receipt.event_context_ref = blob.blob_id
        receipt.event_context_integrity_hash = blob.integrity_hash
        receipt.event_context_size_bytes = blob.size_bytes
        receipt.event_context_content_type = blob.content_type
        receipt.event_context_key_version = blob.wrapping_key_version
        self.session.flush()
        return receipt

    def mark_event_context_unavailable(
        self,
        *,
        receipt: ProviderEventReceipt,
    ) -> tuple[ProviderEventReceipt, ProviderEventBlob | None]:
        """Mark one receipt's event context unavailable."""

        if not receipt.event_context_ref:
            return receipt, None

        blob = self.session.execute(
            select(ProviderEventBlob).where(
                ProviderEventBlob.blob_id == receipt.event_context_ref,
            ),
        ).scalar_one_or_none()
        if blob is None:
            receipt.event_context_ref = None
            receipt.event_context_integrity_hash = None
            receipt.event_context_size_bytes = None
            receipt.event_context_content_type = None
            receipt.event_context_key_version = None
            self.session.flush()
            return receipt, None

        now = datetime.now(timezone.utc)
        blob.commit_state = BlobCommitState.unavailable.value
        blob.unavailable_at = now
        receipt.event_context_ref = None
        receipt.event_context_integrity_hash = None
        receipt.event_context_size_bytes = None
        receipt.event_context_content_type = None
        receipt.event_context_key_version = None
        self.session.flush()
        return receipt, blob

    def get_event_blob_for_authorized_read(
        self,
        *,
        assistant_id: int,
        task_id: int,
        receipt_id: str,
    ) -> tuple[
        ProviderEventBlob | None,
        ProviderEventReceipt | None,
        EventTriggerBinding | None,
    ]:
        """Return one committed blob when the caller owns the receipt."""

        receipt = self.session.execute(
            select(ProviderEventReceipt).where(
                ProviderEventReceipt.receipt_id == receipt_id,
            ),
        ).scalar_one_or_none()
        if receipt is None:
            return None, None, None

        binding = self.get_binding(binding_id=receipt.binding_id)
        if binding is None:
            return None, receipt, None
        if binding.assistant_id != assistant_id or binding.task_id != task_id:
            return None, receipt, binding

        if not receipt.event_context_ref:
            return None, receipt, binding

        blob = self.session.execute(
            select(ProviderEventBlob).where(
                ProviderEventBlob.blob_id == receipt.event_context_ref,
            ),
        ).scalar_one_or_none()
        if blob is None or blob.commit_state != BlobCommitState.committed.value:
            return None, receipt, binding
        return blob, receipt, binding

    def record_blob_audit(
        self,
        *,
        action: BlobAuditAction,
        actor: str,
        blob: ProviderEventBlob | None = None,
        receipt: ProviderEventReceipt | None = None,
        audience: str | None = None,
        assistant_id: int | None = None,
        task_id: int | None = None,
        receipt_id: str | None = None,
        reason: str | None = None,
    ) -> ProviderEventBlobAudit:
        """Append one audit record for private blob access."""

        audit = ProviderEventBlobAudit(
            action=action.value,
            actor=actor,
            audience=audience,
            blob_id=blob.blob_id if blob is not None else None,
            binding_id=(
                blob.binding_id
                if blob is not None
                else receipt.binding_id if receipt is not None else None
            ),
            receipt_id=(
                blob.receipt_id
                if blob is not None
                else receipt.receipt_id if receipt is not None else receipt_id
            ),
            assistant_id=assistant_id,
            task_id=task_id,
            reason=reason,
        )
        self.session.add(audit)
        self.session.flush()
        return audit

    def enqueue_blob_deletion(
        self,
        *,
        blob: ProviderEventBlob,
    ) -> ProviderEventBlobDeletion:
        """Queue physical deletion for one unavailable blob."""

        existing = self.session.execute(
            select(ProviderEventBlobDeletion).where(
                ProviderEventBlobDeletion.blob_id == blob.blob_id,
            ),
        ).scalar_one_or_none()
        if existing is not None:
            return existing

        deletion = ProviderEventBlobDeletion(
            blob_id=blob.blob_id,
            namespace_key=blob.namespace_key,
            processing_state=BlobDeletionState.pending.value,
            next_retry_at=datetime.now(timezone.utc),
        )
        self.session.add(deletion)
        self.session.flush()
        return deletion

    def _claimable_binding_filter(self, now: datetime):
        """Bindings due for reconcile or with an expired reconcile lease."""

        due = and_(
            EventTriggerBinding.reconcile_next_retry_at.isnot(None),
            EventTriggerBinding.reconcile_next_retry_at <= now,
        )
        lease_available = or_(
            EventTriggerBinding.reconcile_lease_expires_at.is_(None),
            EventTriggerBinding.reconcile_lease_expires_at <= now,
        )
        stale_cutoff = now - STALE_RECONCILE_PROCESSING
        stale_processing = and_(
            EventTriggerBinding.reconcile_processing_started_at.isnot(None),
            EventTriggerBinding.reconcile_processing_started_at <= stale_cutoff,
            EventTriggerBinding.reconcile_lease_expires_at.is_(None),
        )
        return and_(due, or_(lease_available, stale_processing))

    def claim_bindings_for_reconcile(
        self,
        *,
        lease_owner: str,
        limit: int,
        lease_ttl: timedelta,
    ) -> list[EventTriggerBinding]:
        """Claim a batch of bindings for reconciliation."""

        now = datetime.now(timezone.utc)
        claimable = (
            select(EventTriggerBinding)
            .where(self._claimable_binding_filter(now))
            .order_by(EventTriggerBinding.reconcile_next_retry_at.asc().nullsfirst())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        bindings = list(self.session.execute(claimable).scalars())
        lease_expires_at = now + lease_ttl
        for binding in bindings:
            binding.reconcile_lease_owner = lease_owner
            binding.reconcile_lease_expires_at = lease_expires_at
            binding.reconcile_processing_started_at = now
        if bindings:
            self.session.flush()
        return bindings

    def release_binding_reconcile_lease(
        self,
        *,
        binding: EventTriggerBinding,
    ) -> None:
        """Clear the reconcile lease on one binding."""

        binding.reconcile_lease_owner = None
        binding.reconcile_lease_expires_at = None
        binding.reconcile_processing_started_at = None
        self.session.flush()

    def schedule_binding_reconcile(
        self,
        *,
        binding: EventTriggerBinding,
        retry_at: datetime | None = None,
        increment_attempt: bool = False,
    ) -> EventTriggerBinding:
        """Schedule a future reconcile attempt for one binding."""

        binding.reconcile_next_retry_at = retry_at or datetime.now(timezone.utc)
        if increment_attempt:
            binding.reconcile_attempt_count += 1
        self.release_binding_reconcile_lease(binding=binding)
        return binding

    def close_acceptance(self, *, binding: EventTriggerBinding) -> EventTriggerBinding:
        """Close local acceptance and end coverage when no trusted generation."""

        binding.local_acceptance_open = False
        if binding.coverage_started_at and binding.coverage_ended_at is None:
            binding.coverage_ended_at = datetime.now(timezone.utc)
        self.session.flush()
        return binding

    def list_generations_for_binding(
        self,
        *,
        binding_id: str,
    ) -> list[EventTriggerSubscriptionGeneration]:
        """Return all generations for one binding ordered by creation."""

        rows = self.session.execute(
            select(EventTriggerSubscriptionGeneration)
            .where(EventTriggerSubscriptionGeneration.binding_id == binding_id)
            .order_by(EventTriggerSubscriptionGeneration.id.asc()),
        ).scalars()
        return list(rows)

    def get_matching_generation(
        self,
        *,
        binding: EventTriggerBinding,
    ) -> EventTriggerSubscriptionGeneration | None:
        """Return the current desired generation if it is provisioning or active."""

        rows = self.list_generations_for_binding(binding_id=binding.binding_id)
        for generation in reversed(rows):
            if (
                generation.desired_activation_revision
                == binding.desired_activation_revision
                and generation.acceptance_epoch == binding.acceptance_epoch
                and generation.lifecycle_state
                in {
                    GenerationLifecycle.provisioning.value,
                    GenerationLifecycle.active.value,
                }
            ):
                return generation
        return None

    def _claimable_generation_filter(self, now: datetime):
        """Generations with due create/delete operations and available leases."""

        operation_due = or_(
            EventTriggerSubscriptionGeneration.create_operation_state.in_(
                [
                    GenerationOperationState.pending.value,
                    GenerationOperationState.retryable.value,
                ],
            ),
            EventTriggerSubscriptionGeneration.delete_operation_state.in_(
                [
                    GenerationOperationState.pending.value,
                    GenerationOperationState.retryable.value,
                ],
            ),
        )
        retry_due = or_(
            EventTriggerSubscriptionGeneration.next_retry_at.is_(None),
            EventTriggerSubscriptionGeneration.next_retry_at <= now,
        )
        lease_available = or_(
            EventTriggerSubscriptionGeneration.lease_expires_at.is_(None),
            EventTriggerSubscriptionGeneration.lease_expires_at <= now,
        )
        return and_(operation_due, retry_due, lease_available)

    def claim_generations_for_operation(
        self,
        *,
        lease_owner: str,
        limit: int,
        lease_ttl: timedelta,
    ) -> list[EventTriggerSubscriptionGeneration]:
        """Claim a batch of generations for provider create/delete work."""

        now = datetime.now(timezone.utc)
        claimable = (
            select(EventTriggerSubscriptionGeneration)
            .where(self._claimable_generation_filter(now))
            .order_by(
                EventTriggerSubscriptionGeneration.next_retry_at.asc().nullsfirst(),
            )
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        generations = list(self.session.execute(claimable).scalars())
        lease_expires_at = now + lease_ttl
        for generation in generations:
            generation.lease_owner = lease_owner
            generation.lease_expires_at = lease_expires_at
            generation.attempt_count += 1
            if generation.create_operation_state in {
                GenerationOperationState.pending.value,
                GenerationOperationState.retryable.value,
            }:
                generation.create_operation_state = (
                    GenerationOperationState.claimed.value
                )
            if generation.delete_operation_state in {
                GenerationOperationState.pending.value,
                GenerationOperationState.retryable.value,
            }:
                generation.delete_operation_state = (
                    GenerationOperationState.claimed.value
                )
        if generations:
            self.session.flush()
        return generations

    def release_generation_lease(
        self,
        *,
        generation: EventTriggerSubscriptionGeneration,
    ) -> None:
        """Clear the operation lease on one generation."""

        generation.lease_owner = None
        generation.lease_expires_at = None
        self.session.flush()

    def journal_generation_create(
        self,
        *,
        generation: EventTriggerSubscriptionGeneration,
        external_trigger_id: str,
        signing_secret_ref: str | None = None,
        signing_secret_version: str | None = None,
    ) -> EventTriggerSubscriptionGeneration:
        """Persist a successful provider create before promotion."""

        now = datetime.now(timezone.utc)
        generation.external_trigger_id = external_trigger_id
        generation.signing_secret_ref = signing_secret_ref
        generation.signing_secret_version = signing_secret_version
        generation.provider_confirmed_at = now
        generation.create_operation_state = GenerationOperationState.succeeded.value
        generation.last_stable_error_code = None
        self.session.flush()
        return generation

    def mark_generation_create_retryable(
        self,
        *,
        generation: EventTriggerSubscriptionGeneration,
        error_code: str,
        retry_at: datetime,
    ) -> EventTriggerSubscriptionGeneration:
        """Record a retryable create failure for one generation."""

        generation.create_operation_state = GenerationOperationState.retryable.value
        generation.last_stable_error_code = error_code
        generation.next_retry_at = retry_at
        self.release_generation_lease(generation=generation)
        return generation

    def mark_generation_create_failed(
        self,
        *,
        generation: EventTriggerSubscriptionGeneration,
        error_code: str,
    ) -> EventTriggerSubscriptionGeneration:
        """Record a terminal create failure for one generation."""

        generation.create_operation_state = GenerationOperationState.failed.value
        generation.lifecycle_state = GenerationLifecycle.failed.value
        generation.last_stable_error_code = error_code
        self.release_generation_lease(generation=generation)
        return generation

    def enqueue_generation_delete(
        self,
        *,
        generation: EventTriggerSubscriptionGeneration,
    ) -> EventTriggerSubscriptionGeneration:
        """Queue provider teardown for one generation."""

        if not generation.provider_delete_idempotency_key:
            generation.provider_delete_idempotency_key = (
                f"delete-{generation.generation_id}"
            )
        if generation.lifecycle_state == GenerationLifecycle.active.value:
            generation.lifecycle_state = GenerationLifecycle.draining.value
            generation.deactivated_at = datetime.now(timezone.utc)
        generation.create_operation_state = None
        generation.delete_operation_state = GenerationOperationState.pending.value
        generation.next_retry_at = datetime.now(timezone.utc)
        self.session.flush()
        return generation

    def journal_generation_delete(
        self,
        *,
        generation: EventTriggerSubscriptionGeneration,
    ) -> EventTriggerSubscriptionGeneration:
        """Persist a successful provider delete."""

        now = datetime.now(timezone.utc)
        generation.delete_operation_state = GenerationOperationState.succeeded.value
        generation.lifecycle_state = GenerationLifecycle.removed.value
        generation.teardown_completed_at = now
        generation.last_stable_error_code = None
        self.release_generation_lease(generation=generation)
        self.session.flush()
        return generation

    def mark_generation_delete_retryable(
        self,
        *,
        generation: EventTriggerSubscriptionGeneration,
        error_code: str,
        retry_at: datetime,
    ) -> EventTriggerSubscriptionGeneration:
        """Record a retryable delete failure for one generation."""

        generation.lifecycle_state = GenerationLifecycle.removing.value
        generation.delete_operation_state = GenerationOperationState.retryable.value
        generation.last_stable_error_code = error_code
        generation.next_retry_at = retry_at
        self.release_generation_lease(generation=generation)
        return generation

    def mark_generation_delete_failed(
        self,
        *,
        generation: EventTriggerSubscriptionGeneration,
        error_code: str,
    ) -> EventTriggerSubscriptionGeneration:
        """Record a terminal delete failure for one generation."""

        generation.delete_operation_state = GenerationOperationState.failed.value
        generation.last_stable_error_code = error_code
        self.release_generation_lease(generation=generation)
        return generation

    def clear_active_generation_if_matches(
        self,
        *,
        binding: EventTriggerBinding,
        generation: EventTriggerSubscriptionGeneration,
    ) -> None:
        """Clear the binding pointer when the removed generation was active."""

        if binding.active_generation_id == generation.generation_id:
            binding.active_generation_id = None
            binding.local_acceptance_open = False
            if binding.coverage_started_at and binding.coverage_ended_at is None:
                binding.coverage_ended_at = datetime.now(timezone.utc)
            self.session.flush()

    def mark_binding_teardown_complete(
        self,
        *,
        binding: EventTriggerBinding,
    ) -> EventTriggerBinding:
        """Mark tombstone teardown complete when no live generations remain."""

        live_states = {
            GenerationLifecycle.provisioning.value,
            GenerationLifecycle.active.value,
            GenerationLifecycle.draining.value,
            GenerationLifecycle.removing.value,
        }
        for generation in self.list_generations_for_binding(
            binding_id=binding.binding_id,
        ):
            if generation.lifecycle_state in live_states:
                return binding
        binding.teardown_completed_at = datetime.now(timezone.utc)
        self.session.flush()
        return binding

    def list_bindings_for_health(
        self,
        *,
        limit: int,
        health_interval: timedelta,
    ) -> list[EventTriggerBinding]:
        """Return bindings due for a provider health check."""

        now = datetime.now(timezone.utc)
        cutoff = now - health_interval
        rows = self.session.execute(
            select(EventTriggerBinding)
            .where(
                EventTriggerBinding.tombstoned_at.is_(None),
                EventTriggerBinding.desired_trigger_state
                == DesiredTriggerState.enabled.value,
                EventTriggerBinding.runtime_health.in_(
                    [
                        BindingRuntimeHealth.healthy.value,
                        BindingRuntimeHealth.recovering.value,
                    ],
                ),
                or_(
                    EventTriggerBinding.last_health_check_at.is_(None),
                    EventTriggerBinding.last_health_check_at <= cutoff,
                ),
            )
            .order_by(EventTriggerBinding.last_health_check_at.asc().nullsfirst())
            .limit(limit),
        ).scalars()
        return list(rows)

    def record_worker_heartbeat(
        self,
        *,
        worker_key: str,
        lease_owner: str,
        last_reconcile_at: datetime | None = None,
        last_health_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ProviderTriggerWorkerHeartbeat:
        """Upsert the provider-trigger worker heartbeat."""

        now = datetime.now(timezone.utc)
        heartbeat = self.session.execute(
            select(ProviderTriggerWorkerHeartbeat).where(
                ProviderTriggerWorkerHeartbeat.worker_key == worker_key,
            ),
        ).scalar_one_or_none()
        if heartbeat is None:
            heartbeat = ProviderTriggerWorkerHeartbeat(
                worker_key=worker_key,
                lease_owner=lease_owner,
                last_reconcile_at=last_reconcile_at,
                last_health_at=last_health_at,
                last_heartbeat_at=now,
                metadata_json=metadata or {},
            )
            self.session.add(heartbeat)
        else:
            heartbeat.lease_owner = lease_owner
            if last_reconcile_at is not None:
                heartbeat.last_reconcile_at = last_reconcile_at
            if last_health_at is not None:
                heartbeat.last_health_at = last_health_at
            heartbeat.last_heartbeat_at = now
            if metadata is not None:
                heartbeat.metadata_json = metadata
        self.session.flush()
        return heartbeat

    def get_worker_heartbeat(
        self,
        *,
        worker_key: str,
    ) -> ProviderTriggerWorkerHeartbeat | None:
        """Return the latest heartbeat row for one worker key."""

        return self.session.execute(
            select(ProviderTriggerWorkerHeartbeat).where(
                ProviderTriggerWorkerHeartbeat.worker_key == worker_key,
            ),
        ).scalar_one_or_none()
