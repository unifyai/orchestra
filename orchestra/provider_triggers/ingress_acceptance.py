"""Signed provider-trigger ingress and transactional event acceptance."""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy.orm import Session

from orchestra.db.dao.integration_provider_dao import IntegrationProviderDAO
from orchestra.db.dao.provider_trigger_dao import ProviderTriggerDAO
from orchestra.db.models.orchestra_models import LogEvent
from orchestra.db.models.provider_trigger_models import (
    EventTriggerBinding,
    EventTriggerSubscriptionGeneration,
    ProviderEventReceipt,
)
from orchestra.observability.provider_trigger_metrics import (
    record_event_to_visible_run_latency,
)
from orchestra.provider_triggers.dispatch_request import (
    COMMUNICATION_DISPATCH_AUDIENCE,
    UNITY_DISPATCH_AUDIENCE,
)
from orchestra.provider_triggers.provider_identity import (
    binding_event_identity_hmac,
    provider_account_subject_hmac,
)
from orchestra.provider_triggers.run_key import build_provider_event_run_key
from orchestra.provider_triggers.runtime_types import (
    DesiredTriggerState,
    GenerationLifecycle,
    ReceiptClassificationReason,
    ReceiptProcessingState,
)
from orchestra.provider_triggers.signing_secret_refs import (
    accepted_signing_secrets_for_generation,
)
from orchestra.provider_triggers.trigger_adapter import (
    NormalizedProviderDelivery,
    TriggerProviderAdapter,
)
from orchestra.provider_triggers.trigger_adapter_registry import (
    get_trigger_provider_adapter,
)
from orchestra.services.provider_event_blob_service import ProviderEventBlobService
from orchestra.services.provider_event_context_service import (
    ProviderEventContextService,
)
from orchestra.services.task_machine_state_service import create_task_run_if_absent
from orchestra.settings import settings

logger = logging.getLogger(__name__)


class IngressAuthenticationError(Exception):
    """Raised when a delivery fails signature or lookup authentication."""


class IngressRetryableError(Exception):
    """Raised when acceptance fails before a durable ignored/accepted outcome."""


@dataclass(frozen=True)
class IngressAcceptanceResult:
    """Durable outcome of one verified provider delivery."""

    status: str
    receipt_id: str
    classification_reason: str
    binding_id: str
    run_id: int | None = None
    run_key: str | None = None
    operation_id: str | None = None


def process_provider_webhook_delivery(
    session: Session,
    *,
    backend_id: str,
    ingress_key: str | None,
    headers: Mapping[str, str],
    raw_body: bytes,
    adapter: TriggerProviderAdapter | None = None,
) -> IngressAcceptanceResult:
    """Verify, classify, and durably accept or ignore one provider delivery.

    Acknowledges only after a durable ignored tombstone or an accepted
    receipt/run/dispatch commit point is reached inside the caller's session.
    """

    dao = ProviderTriggerDAO(session)
    trigger_adapter = adapter or get_trigger_provider_adapter(backend_id)

    if ingress_key is not None:
        resolved = dao.get_generation_by_ingress_key(
            backend_id=backend_id,
            ingress_key=ingress_key,
        )
        unknown_reason = "unknown_ingress_key"
    else:
        if not trigger_adapter.verify_unrouted_delivery(
            headers=headers,
            raw_body=raw_body,
        ):
            logger.info(
                {
                    "event": "provider_trigger_ingress_auth_failed",
                    "reason": "signature_invalid",
                    "backend_id": backend_id,
                },
            )
            raise IngressAuthenticationError("authentication_failed")
        try:
            external_trigger_id = trigger_adapter.delivery_external_trigger_id(
                headers=headers,
                raw_body=raw_body,
            )
        except (UnicodeDecodeError, ValueError):
            logger.info(
                {
                    "event": "provider_trigger_ingress_auth_failed",
                    "reason": "unroutable_delivery",
                    "backend_id": backend_id,
                },
            )
            raise IngressAuthenticationError("authentication_failed") from None
        if not external_trigger_id:
            raise IngressAuthenticationError("authentication_failed")
        resolved = dao.get_generation_by_external_trigger_id(
            backend_id=backend_id,
            external_trigger_id=external_trigger_id,
        )
        unknown_reason = "unknown_external_trigger_id"

    if resolved is None:
        logger.info(
            {
                "event": "provider_trigger_ingress_auth_failed",
                "reason": unknown_reason,
                "backend_id": backend_id,
            },
        )
        raise IngressAuthenticationError("authentication_failed")

    generation, binding = resolved
    secrets = accepted_signing_secrets_for_generation(generation)
    if not trigger_adapter.verify_delivery(
        headers=headers,
        raw_body=raw_body,
        signing_secrets=secrets,
    ):
        logger.info(
            {
                "event": "provider_trigger_ingress_auth_failed",
                "reason": "signature_invalid",
                "backend_id": backend_id,
                "binding_id": binding.binding_id,
                "generation_id": generation.generation_id,
            },
        )
        raise IngressAuthenticationError("authentication_failed")

    try:
        delivery = trigger_adapter.normalize_delivery(
            headers=headers,
            raw_body=raw_body,
        )
    except ValueError as exc:
        unsupported_identity = "unsupported:" + hashlib.sha256(raw_body).hexdigest()
        return _ignore_delivery(
            session,
            binding=binding,
            generation=generation,
            provider_event_identity=unsupported_identity,
            classification=ReceiptClassificationReason.unsupported,
            detail={"normalize_error": str(exc)},
            adapter=trigger_adapter,
            raw_body=raw_body,
        )

    classification = _classify_delivery(
        session,
        binding=binding,
        generation=generation,
        delivery=delivery,
        adapter=trigger_adapter,
    )
    if classification is not ReceiptClassificationReason.matched:
        return _ignore_delivery(
            session,
            binding=binding,
            generation=generation,
            provider_event_identity=delivery.provider_event_identity,
            classification=classification,
            detail={
                "external_trigger_id": delivery.external_trigger_id,
            },
            delivery=delivery,
            adapter=trigger_adapter,
            raw_body=raw_body,
        )

    return _accept_matched_delivery(
        session,
        binding=binding,
        generation=generation,
        delivery=delivery,
        raw_body=raw_body,
        adapter=trigger_adapter,
    )


def _classify_delivery(
    session: Session,
    *,
    binding: EventTriggerBinding,
    generation: EventTriggerSubscriptionGeneration,
    delivery: NormalizedProviderDelivery,
    adapter: TriggerProviderAdapter,
) -> ReceiptClassificationReason:
    if binding.tombstoned_at is not None:
        return ReceiptClassificationReason.inactive
    if binding.desired_trigger_state != DesiredTriggerState.enabled.value:
        return ReceiptClassificationReason.inactive
    if not binding.local_acceptance_open:
        return ReceiptClassificationReason.inactive
    if generation.lifecycle_state != GenerationLifecycle.active.value:
        return ReceiptClassificationReason.stale
    if generation.desired_revision != binding.desired_revision:
        return ReceiptClassificationReason.stale
    if generation.acceptance_epoch != binding.acceptance_epoch:
        return ReceiptClassificationReason.stale
    if binding.active_generation_id != generation.generation_id:
        return ReceiptClassificationReason.stale

    connection = IntegrationProviderDAO(session).get_connection(binding.connection_id)
    if connection is None or not connection.provider_connection_id:
        return ReceiptClassificationReason.unauthorized
    if binding.owner_scope == "assistant":
        if connection.assistant_id != binding.assistant_id:
            return ReceiptClassificationReason.unauthorized
    if connection.backend_id != binding.backend_id:
        return ReceiptClassificationReason.unauthorized
    if connection.canonical_app_slug != binding.canonical_app_slug:
        return ReceiptClassificationReason.unauthorized

    if binding.provider_account_subject_hmac and delivery.provider_user_id:
        pepper = settings.trigger_event_wrapping_master_key or ""
        if pepper:
            delivery_subject_hmac = provider_account_subject_hmac(
                delivery.provider_user_id,
                pepper=pepper,
            )
            if delivery_subject_hmac != binding.provider_account_subject_hmac:
                return ReceiptClassificationReason.unauthorized

    auth_error = adapter.authorize_delivery(
        delivery=delivery,
        expected_connected_account_id=connection.provider_connection_id,
        expected_external_trigger_id=generation.external_trigger_id,
        expected_provider_user_id=connection.provider_user_id,
    )
    if auth_error is not None:
        return ReceiptClassificationReason.unauthorized

    # Graph lifecycle notifications (subscriptionRemoved, missed,
    # reauthorizationRequired) share the Adapters ingress path but must not
    # create provider_event runs.
    source = delivery.source_body or {}
    nested = source.get("data") if isinstance(source.get("data"), Mapping) else {}
    if source.get("lifecycle_event") or (
        isinstance(nested, Mapping) and nested.get("lifecycle_event")
    ):
        return ReceiptClassificationReason.unsupported

    return ReceiptClassificationReason.matched


def _ignore_delivery(
    session: Session,
    *,
    binding: EventTriggerBinding,
    generation: EventTriggerSubscriptionGeneration,
    provider_event_identity: str,
    classification: ReceiptClassificationReason,
    detail: Mapping[str, Any] | None = None,
    delivery: NormalizedProviderDelivery | None = None,
    adapter: TriggerProviderAdapter | None = None,
    raw_body: bytes | None = None,
) -> IngressAcceptanceResult:
    dao = ProviderTriggerDAO(session)
    locked_binding = dao.get_binding(binding_id=binding.binding_id, for_update=True)
    if locked_binding is None:
        raise IngressRetryableError("binding_missing_during_ignore")
    locked_generation = dao.get_generation(
        generation_id=generation.generation_id,
        for_update=True,
    )
    if locked_generation is None:
        raise IngressRetryableError("generation_missing_during_ignore")

    if delivery is not None and adapter is not None:
        classification = _classify_delivery(
            session,
            binding=locked_binding,
            generation=locked_generation,
            delivery=delivery,
            adapter=adapter,
        )
        if classification is ReceiptClassificationReason.matched:
            if raw_body is None:
                raise IngressRetryableError("matched_delivery_missing_raw_body")
            return _accept_matched_delivery(
                session,
                binding=locked_binding,
                generation=locked_generation,
                delivery=delivery,
                raw_body=raw_body,
                adapter=adapter,
            )

    identity_hmac = _identity_hmac_for_binding(
        session,
        binding=locked_binding,
        provider_event_identity=provider_event_identity,
    )
    existing = dao.get_receipt_by_identity(
        binding_id=locked_binding.binding_id,
        provider_event_identity_hmac=identity_hmac,
    )
    if existing is not None:
        return _result_from_existing_receipt(
            existing,
            binding_id=locked_binding.binding_id,
        )

    receipt = dao.adopt_receipt(
        binding=locked_binding,
        generation=locked_generation,
        provider_event_identity_hmac=identity_hmac,
        processing_state=ReceiptProcessingState.ignored.value,
        classification_reason=classification.value,
        acceptance_authorization_json={
            "classification": classification.value,
            "detail": dict(detail or {}),
        },
    )
    logger.info(
        {
            "event": "provider_trigger_ingress_ignored",
            "binding_id": locked_binding.binding_id,
            "generation_id": locked_generation.generation_id,
            "receipt_id": receipt.receipt_id,
            "classification_reason": classification.value,
        },
    )
    return IngressAcceptanceResult(
        status="ignored",
        receipt_id=receipt.receipt_id,
        classification_reason=classification.value,
        binding_id=locked_binding.binding_id,
    )


def _accept_matched_delivery(
    session: Session,
    *,
    binding: EventTriggerBinding,
    generation: EventTriggerSubscriptionGeneration,
    delivery: NormalizedProviderDelivery,
    raw_body: bytes,
    adapter: TriggerProviderAdapter,
) -> IngressAcceptanceResult:
    dao = ProviderTriggerDAO(session)
    blob_service = ProviderEventBlobService(session)
    identity_hmac = _identity_hmac_for_binding(
        session,
        binding=binding,
        provider_event_identity=delivery.provider_event_identity,
    )
    existing_before_write = dao.get_receipt_by_identity(
        binding_id=binding.binding_id,
        provider_event_identity_hmac=identity_hmac,
    )
    if existing_before_write is not None:
        return _result_from_existing_receipt(
            existing_before_write,
            binding_id=binding.binding_id,
        )

    candidate_receipt_id = f"receipt-{uuid.uuid4().hex[:12]}"

    # Encrypt/store outside the acceptance lock; orphan cleanup removes
    # unattached ciphertext if acceptance later adopts a duplicate.
    uncommitted_blob = blob_service.write_uncommitted(
        binding_id=binding.binding_id,
        receipt_id=candidate_receipt_id,
        plaintext=raw_body,
        content_type="application/json",
        actor="provider-trigger-ingress",
    )
    session.flush()

    locked_binding = dao.get_binding(binding_id=binding.binding_id, for_update=True)
    if locked_binding is None:
        raise IngressRetryableError("binding_missing_during_accept")
    locked_generation = dao.get_generation(
        generation_id=generation.generation_id,
        for_update=True,
    )
    if locked_generation is None:
        raise IngressRetryableError("generation_missing_during_accept")

    reclass = _classify_delivery(
        session,
        binding=locked_binding,
        generation=locked_generation,
        delivery=delivery,
        adapter=adapter,
    )
    if reclass is not ReceiptClassificationReason.matched:
        return _ignore_under_lock(
            dao=dao,
            binding=locked_binding,
            generation=locked_generation,
            delivery=delivery,
            classification=reclass,
        )

    identity_hmac = _identity_hmac_for_binding(
        session,
        binding=locked_binding,
        provider_event_identity=delivery.provider_event_identity,
    )
    existing = dao.get_receipt_by_identity(
        binding_id=locked_binding.binding_id,
        provider_event_identity_hmac=identity_hmac,
    )
    if existing is not None:
        return _result_from_existing_receipt(
            existing,
            binding_id=locked_binding.binding_id,
        )

    authorization = {
        "classification": ReceiptClassificationReason.matched.value,
        "accepted_revision": locked_generation.desired_revision,
        "acceptance_epoch": locked_generation.acceptance_epoch,
        "generation_id": locked_generation.generation_id,
        "provider_event_identity": delivery.provider_event_identity,
        "external_trigger_id": delivery.external_trigger_id,
    }
    receipt = dao.adopt_receipt(
        binding=locked_binding,
        generation=locked_generation,
        provider_event_identity_hmac=identity_hmac,
        receipt_id=candidate_receipt_id,
        acceptance_authorization_json=authorization,
        processing_state=ReceiptProcessingState.accepted.value,
        classification_reason=ReceiptClassificationReason.matched.value,
        stable_envelope_json=dict(delivery.envelope),
        curated_projection_json=None,
    )
    if receipt.receipt_id != candidate_receipt_id:
        return _result_from_existing_receipt(
            receipt,
            binding_id=locked_binding.binding_id,
        )

    blob_service.attach_event_context(
        receipt=receipt,
        blob=uncommitted_blob,
        actor="provider-trigger-ingress",
    )

    run_delivery = "offline" if locked_binding.execution_mode == "offline" else "live"
    run_key = build_provider_event_run_key(
        assistant_id=str(locked_binding.assistant_id),
        task_id=locked_binding.task_id,
        binding_id=locked_binding.binding_id,
        revision=receipt.accepted_revision,
        event_identity_hmac=identity_hmac,
        delivery=run_delivery,  # type: ignore[arg-type]
    )
    received_at = datetime.now(timezone.utc).isoformat()
    acceptance_started_at = datetime.now(timezone.utc)
    task_row = (
        session.query(LogEvent)
        .filter(
            LogEvent.project_id == locked_binding.project_id,
            LogEvent.id == locked_binding.source_task_log_id,
        )
        .one_or_none()
    )
    task_data: dict[str, Any] = (
        dict(task_row.data or {}) if task_row is not None else {}
    )
    task_name = str(task_data.get("name")) if task_data.get("name") else None
    task_description = (
        str(task_data.get("description")) if task_data.get("description") else None
    )
    run_payload = {
        "run_key": run_key,
        "assistant_id": str(locked_binding.assistant_id),
        "task_id": locked_binding.task_id,
        "source_task_log_id": locked_binding.source_task_log_id,
        "task_name": task_name,
        "task_description": task_description,
        "wake": "provider_event",
        "delivery": run_delivery,
        "state": "pending",
        "revision": receipt.accepted_revision,
        "provider_event_binding_id": locked_binding.binding_id,
        "provider_event_receipt_id": receipt.receipt_id,
        "provider_event_backend_id": locked_binding.backend_id,
        "provider_event_app_slug": locked_binding.canonical_app_slug,
        "provider_event_slug": locked_binding.provider_trigger_slug,
        "provider_event_schema_version": "0",
        "provider_event_acceptance_epoch": receipt.acceptance_epoch,
        "provider_event_received_at": received_at,
        "provider_event_occurred_at": delivery.occurred_at,
        "provider_event_trigger_config": dict(locked_binding.trigger_config_json or {}),
        "provider_event_identity_hmac": identity_hmac,
        "source_ref": delivery.provider_event_identity,
    }
    try:
        run_row, run_created = create_task_run_if_absent(
            session,
            locked_binding.project_id,
            run_payload,
        )
    except Exception as exc:
        raise IngressRetryableError(f"run_create_failed:{exc}") from exc

    run_id = int(run_row.id)
    audience = (
        COMMUNICATION_DISPATCH_AUDIENCE
        if run_delivery == "offline"
        else UNITY_DISPATCH_AUDIENCE
    )
    dispatch = dao.adopt_dispatch(
        receipt=receipt,
        binding=locked_binding,
        run_id=run_id,
        run_key=run_key,
        audience=audience,
    )
    ProviderEventContextService(session).stamp_expiry_on_acceptance(
        receipt=receipt,
        run_key=run_key,
        project_id=locked_binding.project_id,
        assistant_id=locked_binding.assistant_id,
        source_task_log_id=locked_binding.source_task_log_id,
    )
    logger.info(
        {
            "event": "provider_trigger_ingress_accepted",
            "binding_id": locked_binding.binding_id,
            "generation_id": locked_generation.generation_id,
            "receipt_id": receipt.receipt_id,
            "run_id": run_id,
            "run_key": run_key,
            "operation_id": dispatch.operation_id,
            "run_created": run_created,
        },
    )
    if run_created:
        record_event_to_visible_run_latency(
            latency_seconds=(
                datetime.now(timezone.utc) - acceptance_started_at
            ).total_seconds(),
        )
    return IngressAcceptanceResult(
        status="accepted",
        receipt_id=receipt.receipt_id,
        classification_reason=ReceiptClassificationReason.matched.value,
        binding_id=locked_binding.binding_id,
        run_id=run_id,
        run_key=run_key,
        operation_id=dispatch.operation_id,
    )


def _ignore_under_lock(
    *,
    dao: ProviderTriggerDAO,
    binding: EventTriggerBinding,
    generation: EventTriggerSubscriptionGeneration,
    delivery: NormalizedProviderDelivery,
    classification: ReceiptClassificationReason,
) -> IngressAcceptanceResult:
    identity_hmac = _identity_hmac_for_binding(
        dao.session,
        binding=binding,
        provider_event_identity=delivery.provider_event_identity,
    )
    existing = dao.get_receipt_by_identity(
        binding_id=binding.binding_id,
        provider_event_identity_hmac=identity_hmac,
    )
    if existing is not None:
        return _result_from_existing_receipt(existing, binding_id=binding.binding_id)
    receipt = dao.adopt_receipt(
        binding=binding,
        generation=generation,
        provider_event_identity_hmac=identity_hmac,
        processing_state=ReceiptProcessingState.ignored.value,
        classification_reason=classification.value,
        acceptance_authorization_json={
            "classification": classification.value,
            "detail": {},
        },
    )
    return IngressAcceptanceResult(
        status="ignored",
        receipt_id=receipt.receipt_id,
        classification_reason=classification.value,
        binding_id=binding.binding_id,
    )


def _result_from_existing_receipt(
    receipt: ProviderEventReceipt,
    *,
    binding_id: str,
) -> IngressAcceptanceResult:
    status = (
        "ignored"
        if receipt.processing_state == ReceiptProcessingState.ignored.value
        else "accepted"
    )
    classification_reason = receipt.classification_reason or (
        ReceiptClassificationReason.matched.value
        if status == "accepted"
        else ReceiptClassificationReason.inactive.value
    )
    return IngressAcceptanceResult(
        status=status,
        receipt_id=receipt.receipt_id,
        classification_reason=classification_reason,
        binding_id=binding_id,
        run_id=receipt.run_id,
        run_key=receipt.run_key,
        operation_id=receipt.dispatch_operation_id,
    )


def _identity_hmac_for_binding(
    session: Session,
    *,
    binding: EventTriggerBinding,
    provider_event_identity: str,
) -> str:
    dedup_key = ProviderEventBlobService(session).ensure_binding_dedup_key(
        binding=binding,
    )
    return binding_event_identity_hmac(
        binding_hmac_key=dedup_key,
        provider_event_identity=provider_event_identity,
    )
