"""Private provider-event storage tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestra.provider_triggers.private_event_storage import (
    AES_GCM_ALGORITHM,
    PRIVATE_EVENT_BUCKET_NAME,
    EncryptedEventObject,
    EventBlobAuthenticationError,
    EventBlobService,
    FilesystemPrivateObjectStore,
    TriggerKeyWrappingService,
    WrappedKeyMaterial,
    blob_associated_data,
    blob_wrap_purpose,
)
from orchestra.provider_triggers.provider_identity import binding_event_identity_hmac
from orchestra.services.local_bucket_service import LocalBucketService
from orchestra.web.api.storage.views import public_router


@pytest.fixture
def private_blob_root(tmp_path: Path) -> Path:
    return tmp_path / "provider-event-private"


@pytest.fixture
def public_blob_root(tmp_path: Path) -> Path:
    return tmp_path / "public-local-bucket"


@pytest.fixture
def wrapping_service() -> TriggerKeyWrappingService:
    return TriggerKeyWrappingService(master_key=b"provider-trigger-self-host-master")


@pytest.fixture
def blob_service(
    private_blob_root: Path,
    wrapping_service: TriggerKeyWrappingService,
) -> EventBlobService:
    return EventBlobService(
        object_store=FilesystemPrivateObjectStore(private_blob_root),
        wrapping_service=wrapping_service,
    )


def test_aes_gcm_event_blob_round_trips(blob_service: EventBlobService) -> None:
    encrypted = blob_service.encrypt_and_store(
        binding_id="binding-1",
        receipt_id="receipt-1",
        plaintext=b'{"title":"fixture"}',
    )
    plaintext = blob_service.read_object(
        encrypted,
        binding_id="binding-1",
        receipt_id="receipt-1",
    )
    assert plaintext == b'{"title":"fixture"}'
    assert encrypted.algorithm == AES_GCM_ALGORITHM
    assert encrypted.integrity_hash
    assert blob_service.bucket_name == PRIVATE_EVENT_BUCKET_NAME


def test_aad_mismatch_fails_closed(blob_service: EventBlobService) -> None:
    encrypted = blob_service.encrypt_and_store(
        binding_id="binding-1",
        receipt_id="receipt-1",
        plaintext=b"secret-provider-payload",
    )
    with pytest.raises(EventBlobAuthenticationError):
        blob_service.read_object(
            encrypted,
            binding_id="binding-1",
            receipt_id="receipt-other",
        )


def test_integrity_mismatch_fails_closed(
    blob_service: EventBlobService,
    private_blob_root: Path,
) -> None:
    encrypted = blob_service.encrypt_and_store(
        binding_id="binding-1",
        receipt_id="receipt-1",
        plaintext=b"secret-provider-payload",
    )
    object_path = private_blob_root / encrypted.namespace_key
    object_path.write_bytes(object_path.read_bytes() + b"tamper")
    with pytest.raises(EventBlobAuthenticationError):
        blob_service.read_object(
            encrypted,
            binding_id="binding-1",
            receipt_id="receipt-1",
        )


def test_rewrap_preserves_plaintext_and_binding_hmac_digest(
    wrapping_service: TriggerKeyWrappingService,
    private_blob_root: Path,
) -> None:
    first_service = EventBlobService(
        object_store=FilesystemPrivateObjectStore(private_blob_root),
        wrapping_service=wrapping_service,
    )
    encrypted = first_service.encrypt_and_store(
        binding_id="binding-1",
        receipt_id="receipt-1",
        plaintext=b"stable-payload",
    )
    binding_key = wrapping_service.generate_binding_hmac_key()
    before = binding_event_identity_hmac(
        binding_hmac_key=binding_key,
        provider_event_identity="provider-event-123",
    )

    rotated_wrapping = TriggerKeyWrappingService(
        master_key=b"provider-trigger-rotated-master",
    )
    purpose = blob_wrap_purpose(binding_id="binding-1", receipt_id="receipt-1")
    old_wrapped = WrappedKeyMaterial(
        wrapping_key_version=encrypted.wrapping_key_version,
        wrapped_key=encrypted.wrapped_data_key,
        algorithm=encrypted.wrap_algorithm,
    )
    data_key = wrapping_service.unwrap_key(old_wrapped, purpose=purpose)
    new_wrapped = rotated_wrapping.wrap_key(purpose=purpose, key_material=data_key)
    rewrapped = EncryptedEventObject(
        namespace_key=encrypted.namespace_key,
        algorithm=encrypted.algorithm,
        wrap_algorithm=new_wrapped.algorithm,
        wrapping_key_version=new_wrapped.wrapping_key_version,
        wrapped_data_key=new_wrapped.wrapped_key,
        integrity_hash=encrypted.integrity_hash,
        size_bytes=encrypted.size_bytes,
        content_type=encrypted.content_type,
    )
    assert rewrapped.wrapping_key_version != encrypted.wrapping_key_version
    assert rewrapped.wrapped_data_key != encrypted.wrapped_data_key
    assert rewrapped.integrity_hash == encrypted.integrity_hash

    rotated_service = EventBlobService(
        object_store=FilesystemPrivateObjectStore(private_blob_root),
        wrapping_service=rotated_wrapping,
    )
    plaintext = rotated_service.read_object(
        rewrapped,
        binding_id="binding-1",
        receipt_id="receipt-1",
    )
    assert plaintext == b"stable-payload"
    after = binding_event_identity_hmac(
        binding_hmac_key=binding_key,
        provider_event_identity="provider-event-123",
    )
    assert before == after


def test_auth_tag_mismatch_fails_closed(
    blob_service: EventBlobService,
    private_blob_root: Path,
) -> None:
    encrypted = blob_service.encrypt_and_store(
        binding_id="binding-1",
        receipt_id="receipt-1",
        plaintext=b"secret-provider-payload",
    )
    ciphertext = (private_blob_root / encrypted.namespace_key).read_bytes()
    corrupted = bytearray(ciphertext)
    corrupted[-1] ^= 0x01
    (private_blob_root / encrypted.namespace_key).write_bytes(bytes(corrupted))
    with pytest.raises(EventBlobAuthenticationError):
        blob_service.read_object(
            encrypted,
            binding_id="binding-1",
            receipt_id="receipt-1",
        )


def test_private_trigger_event_blob_is_unreachable_from_public_local_storage(
    private_blob_root: Path,
    public_blob_root: Path,
    wrapping_service: TriggerKeyWrappingService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SELF_HOST", "1")
    monkeypatch.setenv("SELF_HOST_LOCAL_BUCKET_DIR", str(public_blob_root))

    private_service = EventBlobService(
        object_store=FilesystemPrivateObjectStore(private_blob_root),
        wrapping_service=wrapping_service,
    )
    encrypted = private_service.encrypt_and_store(
        binding_id="binding-1",
        receipt_id="receipt-1",
        plaintext=b"secret-provider-payload",
    )

    bucket_service = LocalBucketService()
    assert not bucket_service.is_allowed_bucket(PRIVATE_EVENT_BUCKET_NAME)
    assert private_blob_root.resolve() != public_blob_root.resolve()

    app = FastAPI()
    app.include_router(public_router, prefix="/v0")
    client = TestClient(app)
    response = client.get(
        f"/v0/storage/local/{PRIVATE_EVENT_BUCKET_NAME}/{encrypted.namespace_key}",
    )
    assert response.status_code == 403


def test_encrypt_object_uses_binding_receipt_aad(
    wrapping_service: TriggerKeyWrappingService,
) -> None:
    purpose = blob_wrap_purpose(binding_id="binding-1", receipt_id="receipt-1")
    associated_data = blob_associated_data(
        binding_id="binding-1",
        receipt_id="receipt-1",
    )
    data_key = wrapping_service.generate_binding_hmac_key()
    wrapped = wrapping_service.wrap_key(purpose=purpose, key_material=data_key)
    unwrapped = wrapping_service.unwrap_key(wrapped, purpose=purpose)
    assert unwrapped == data_key

    nonce = b"\x00" * 12
    ciphertext_body = AESGCM(data_key).encrypt(associated_data, b"payload", nonce)
    with pytest.raises(Exception):
        AESGCM(data_key).decrypt(
            blob_associated_data(binding_id="binding-1", receipt_id="other"),
            ciphertext_body,
            nonce,
        )
