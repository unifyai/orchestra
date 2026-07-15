"""Integration tests for private provider-event blob lifecycle."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.dao.provider_trigger_dao import ProviderTriggerDAO
from orchestra.db.models.provider_trigger_models import (
    EventTriggerBinding,
    EventTriggerSubscriptionGeneration,
    ProviderEventBlob,
    ProviderEventBlobAudit,
    ProviderEventBlobDeletion,
)
from orchestra.provider_triggers.private_event_storage import (
    EventBlobAuthenticationError,
    EventBlobService,
    FilesystemPrivateObjectStore,
    TriggerKeyWrappingService,
    provider_event_storage_configured,
)
from orchestra.provider_triggers.provider_identity import binding_event_identity_hmac
from orchestra.provider_triggers.provider_trigger_mutation import (
    promote_active_generation,
)
from orchestra.provider_triggers.runtime_types import BlobAuditAction, BlobCommitState
from orchestra.services.provider_event_blob_cleanup_service import (
    ProviderEventBlobCleanupService,
)
from orchestra.services.provider_event_blob_service import ProviderEventBlobService
from orchestra.settings import settings
from orchestra.tests.provider_triggers.control_plane_harness import (
    seed_minimal_test_binding,
)


@pytest.fixture
def storage_root(tmp_path: Path) -> Path:
    return Path(settings.trigger_event_private_root)


@pytest.fixture
def wrapping_service() -> TriggerKeyWrappingService:
    return TriggerKeyWrappingService(master_key=b"test-master-key-material")


@pytest.fixture
def blob_service(
    storage_root: Path,
    wrapping_service: TriggerKeyWrappingService,
) -> EventBlobService:
    return EventBlobService(
        object_store=FilesystemPrivateObjectStore(storage_root),
        wrapping_service=wrapping_service,
    )


@pytest.fixture
def blob_lifecycle_service(
    dbsession: Session,
    blob_service: EventBlobService,
    wrapping_service: TriggerKeyWrappingService,
) -> ProviderEventBlobService:
    return ProviderEventBlobService(
        dbsession,
        blob_service=blob_service,
        wrapping_service=wrapping_service,
    )


def _binding_row(dbsession: Session, binding_id: str) -> EventTriggerBinding:
    binding = dbsession.execute(
        select(EventTriggerBinding).where(
            EventTriggerBinding.binding_id == binding_id,
        ),
    ).scalar_one()
    return binding


def _active_generation(
    dbsession: Session,
    binding: EventTriggerBinding,
) -> EventTriggerSubscriptionGeneration:
    assert binding.active_generation_id is not None
    return dbsession.execute(
        select(EventTriggerSubscriptionGeneration).where(
            EventTriggerSubscriptionGeneration.generation_id
            == binding.active_generation_id,
        ),
    ).scalar_one()


def test_storage_prerequisites_gate_is_exposed() -> None:
    assert provider_event_storage_configured() is True


def test_uncommitted_write_attach_and_read_round_trip(
    dbsession: Session,
    blob_lifecycle_service: ProviderEventBlobService,
    storage_root: Path,
) -> None:
    initialized = seed_minimal_test_binding(dbsession, desired_state="enabled")
    promote_active_generation(dbsession, binding_id=initialized.binding_id)
    binding = _binding_row(dbsession, initialized.binding_id)
    generation = _active_generation(dbsession, binding)
    receipt = blob_lifecycle_service._dao.adopt_receipt(
        binding=binding,
        generation=generation,
        provider_event_identity_hmac="event-identity-1",
        receipt_id="receipt-attach-1",
    )

    blob = blob_lifecycle_service.write_uncommitted(
        binding_id=binding.binding_id,
        receipt_id=receipt.receipt_id,
        plaintext=b'{"issue":"opened"}',
    )
    assert blob.commit_state == BlobCommitState.uncommitted.value
    assert (storage_root / blob.namespace_key).exists()

    attached = blob_lifecycle_service.attach_event_context(receipt=receipt, blob=blob)
    assert attached.event_context_ref == blob.blob_id
    assert attached.event_context_integrity_hash == blob.integrity_hash
    assert blob.commit_state == BlobCommitState.committed.value

    plaintext = blob_lifecycle_service.read_authorized(
        assistant_id=binding.assistant_id,
        task_id=binding.task_id,
        receipt_id=receipt.receipt_id,
        actor="test-user",
    )
    assert plaintext == b'{"issue":"opened"}'

    audits = (
        dbsession.execute(
            select(ProviderEventBlobAudit).where(
                ProviderEventBlobAudit.blob_id == blob.blob_id,
            ),
        )
        .scalars()
        .all()
    )
    actions = {audit.action for audit in audits}
    assert BlobAuditAction.write.value in actions
    assert BlobAuditAction.read.value in actions


def test_orphan_sweep_removes_uncommitted_blob(
    dbsession: Session,
    blob_lifecycle_service: ProviderEventBlobService,
    storage_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "trigger_event_orphan_safety_seconds", 0)
    initialized = seed_minimal_test_binding(dbsession, desired_state="enabled")
    binding = _binding_row(dbsession, initialized.binding_id)
    blob = blob_lifecycle_service.write_uncommitted(
        binding_id=binding.binding_id,
        receipt_id="receipt-orphan-1",
        plaintext=b"orphan-payload",
    )
    blob.created_at = datetime.now(timezone.utc) - timedelta(hours=2)
    dbsession.flush()

    cleanup = ProviderEventBlobCleanupService(
        dbsession,
        blob_service=blob_lifecycle_service._blob_service,
    )
    removed = cleanup.sweep_orphan_uncommitted()
    assert removed == 1
    assert not (storage_root / blob.namespace_key).exists()

    refreshed = dbsession.execute(
        select(ProviderEventBlob).where(ProviderEventBlob.blob_id == blob.blob_id),
    ).scalar_one()
    assert refreshed.commit_state == BlobCommitState.deleted.value


def test_mark_unavailable_queues_deletion_and_blocks_read(
    dbsession: Session,
    blob_lifecycle_service: ProviderEventBlobService,
    storage_root: Path,
) -> None:
    initialized = seed_minimal_test_binding(dbsession, desired_state="enabled")
    binding = _binding_row(dbsession, initialized.binding_id)
    dao = ProviderTriggerDAO(dbsession)
    generation = dao.create_generation(binding=binding)
    receipt = blob_lifecycle_service._dao.adopt_receipt(
        binding=binding,
        generation=generation,
        provider_event_identity_hmac="event-identity-2",
        receipt_id="receipt-delete-1",
    )
    blob = blob_lifecycle_service.write_uncommitted(
        binding_id=binding.binding_id,
        receipt_id=receipt.receipt_id,
        plaintext=b"temporary-payload",
    )
    blob_lifecycle_service.attach_event_context(receipt=receipt, blob=blob)

    blob_lifecycle_service.mark_event_context_unavailable(receipt=receipt)
    deletion = dbsession.execute(
        select(ProviderEventBlobDeletion).where(
            ProviderEventBlobDeletion.blob_id == blob.blob_id,
        ),
    ).scalar_one()
    assert deletion.namespace_key == blob.namespace_key

    cleanup = ProviderEventBlobCleanupService(
        dbsession,
        blob_service=blob_lifecycle_service._blob_service,
    )
    assert cleanup.process_deletion_batch() == 1
    assert not (storage_root / blob.namespace_key).exists()

    with pytest.raises(PermissionError):
        blob_lifecycle_service.read_authorized(
            assistant_id=binding.assistant_id,
            task_id=binding.task_id,
            receipt_id=receipt.receipt_id,
            actor="test-user",
        )


def test_read_denied_is_audited_on_auth_failure(
    dbsession: Session,
    blob_lifecycle_service: ProviderEventBlobService,
    storage_root: Path,
) -> None:
    initialized = seed_minimal_test_binding(dbsession, desired_state="enabled")
    binding = _binding_row(dbsession, initialized.binding_id)
    dao = ProviderTriggerDAO(dbsession)
    generation = dao.create_generation(binding=binding)
    receipt = blob_lifecycle_service._dao.adopt_receipt(
        binding=binding,
        generation=generation,
        provider_event_identity_hmac="event-identity-3",
        receipt_id="receipt-auth-1",
    )
    blob = blob_lifecycle_service.write_uncommitted(
        binding_id=binding.binding_id,
        receipt_id=receipt.receipt_id,
        plaintext=b"protected-payload",
    )
    blob_lifecycle_service.attach_event_context(receipt=receipt, blob=blob)
    (storage_root / blob.namespace_key).write_bytes(b"corrupted")

    with pytest.raises(EventBlobAuthenticationError):
        blob_lifecycle_service.read_authorized(
            assistant_id=binding.assistant_id,
            task_id=binding.task_id,
            receipt_id=receipt.receipt_id,
            actor="test-user",
        )

    denied = dbsession.execute(
        select(ProviderEventBlobAudit).where(
            ProviderEventBlobAudit.blob_id == blob.blob_id,
            ProviderEventBlobAudit.action == BlobAuditAction.read_denied.value,
        ),
    ).scalar_one()
    assert denied.reason == "authentication_failed"


def test_ensure_binding_dedup_key_is_stable(
    dbsession: Session,
    blob_lifecycle_service: ProviderEventBlobService,
) -> None:
    initialized = seed_minimal_test_binding(dbsession, desired_state="enabled")
    binding = _binding_row(dbsession, initialized.binding_id)
    first = blob_lifecycle_service.ensure_binding_dedup_key(binding=binding)
    second = blob_lifecycle_service.ensure_binding_dedup_key(binding=binding)
    assert first == second

    digest = binding_event_identity_hmac(
        binding_hmac_key=first,
        provider_event_identity="provider-event-stable",
    )
    assert digest == binding_event_identity_hmac(
        binding_hmac_key=second,
        provider_event_identity="provider-event-stable",
    )


def test_promote_generation_requires_storage_configuration(
    dbsession: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initialized = seed_minimal_test_binding(dbsession, desired_state="enabled")
    monkeypatch.setattr(settings, "trigger_event_wrapping_master_key", None)
    promote_active_generation(dbsession, binding_id=initialized.binding_id)
    binding = _binding_row(dbsession, initialized.binding_id)
    assert binding.local_acceptance_open is False
    assert binding.runtime_health == "needs_attention"
    assert binding.last_stable_error_code == "event_storage_unconfigured"
