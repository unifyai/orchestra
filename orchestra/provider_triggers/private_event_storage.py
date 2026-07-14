"""Private encrypted storage for provider-event payloads."""

from __future__ import annotations

import base64
import hashlib
import logging
import re
import secrets
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Iterator

from cryptography.exceptions import InvalidTag
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from orchestra.settings import settings

logger = logging.getLogger(__name__)

PRIVATE_EVENT_BUCKET_NAME: Final[str] = "provider-event-blobs"
AES_GCM_ALGORITHM: Final[str] = "aes-gcm-v1"
SELF_HOST_WRAP_ALGORITHM: Final[str] = "fernet-hkdf-v1"
KMS_WRAP_ALGORITHM: Final[str] = "kms-v1"
_NONCE_LENGTH = 12
_SAFE_OBJECT_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class ProviderEventStorageUnavailableError(RuntimeError):
    """Raised when private event storage prerequisites are missing."""


class EventBlobAuthenticationError(ValueError):
    """Raised when ciphertext integrity or authentication checks fail."""


def _validated_object_segment(value: str, *, field_name: str) -> str:
    if not _SAFE_OBJECT_SEGMENT.fullmatch(value):
        raise ValueError(f"invalid {field_name} for private event storage")
    return value


def blob_associated_data(*, binding_id: str, receipt_id: str) -> bytes:
    """Return canonical AES-GCM associated data for one provider-event object."""

    _validated_object_segment(binding_id, field_name="binding_id")
    _validated_object_segment(receipt_id, field_name="receipt_id")
    return f"{binding_id}|{receipt_id}".encode("utf-8")


def blob_wrap_purpose(*, binding_id: str, receipt_id: str) -> str:
    """Return the key-wrapping purpose string for one provider-event object."""

    _validated_object_segment(binding_id, field_name="binding_id")
    _validated_object_segment(receipt_id, field_name="receipt_id")
    return f"blob:{binding_id}:{receipt_id}"


def binding_dedup_wrap_purpose(*, binding_id: str) -> str:
    """Return the key-wrapping purpose string for one binding HMAC key."""

    _validated_object_segment(binding_id, field_name="binding_id")
    return f"binding-dedup:{binding_id}"


def namespace_key_for(*, binding_id: str, receipt_id: str) -> str:
    """Return the private object namespace key for one receipt payload."""

    _validated_object_segment(binding_id, field_name="binding_id")
    _validated_object_segment(receipt_id, field_name="receipt_id")
    return f"{binding_id}/{receipt_id}.bin"


@dataclass(frozen=True)
class WrappedKeyMaterial:
    """Metadata for a wrapped symmetric key."""

    wrapping_key_version: str
    wrapped_key: bytes
    algorithm: str


@dataclass(frozen=True)
class EncryptedEventObject:
    """Encrypted provider-event object metadata and ciphertext location."""

    namespace_key: str
    algorithm: str
    wrap_algorithm: str
    wrapping_key_version: str
    wrapped_data_key: bytes
    integrity_hash: str
    size_bytes: int
    content_type: str


class TriggerKeyWrappingService:
    """Wrap binding HMAC keys and per-object data keys for private event storage."""

    def __init__(
        self,
        *,
        master_key: bytes | None = None,
        use_kms: bool | None = None,
    ) -> None:
        self._use_kms = (
            use_kms
            if use_kms is not None
            else (not settings.is_self_host and self._kms_available())
        )
        if self._use_kms:
            if not self._kms_available():
                raise ProviderEventStorageUnavailableError(
                    "hosted trigger-event key wrapping requires GCP KMS configuration",
                )
            self.wrapping_key_version = self._kms_version_label()
            self._master_key = None
        else:
            material = master_key
            if material is None:
                configured = settings.trigger_event_wrapping_master_key
                if not configured:
                    raise ProviderEventStorageUnavailableError(
                        "TRIGGER_EVENT_WRAPPING_MASTER_KEY is required for self-host "
                        "private event storage",
                    )
                material = configured.encode("utf-8")
            self._master_key = material
            self.wrapping_key_version = hashlib.sha256(material).hexdigest()[:16]

    @classmethod
    def is_configured(cls) -> bool:
        """Return True when key wrapping prerequisites are satisfied."""

        if settings.is_self_host:
            return bool(settings.trigger_event_wrapping_master_key)
        return bool(
            settings.gcp_project
            and settings.gcp_location
            and settings.trigger_event_kms_keyring
            and settings.trigger_event_kms_key,
        )

    @staticmethod
    def _kms_available() -> bool:
        return bool(settings.gcp_project and settings.gcp_location)

    @staticmethod
    def _kms_version_label() -> str:
        return (
            f"kms:{settings.gcp_project}:{settings.gcp_location}:"
            f"{settings.trigger_event_kms_keyring}:{settings.trigger_event_kms_key}"
        )

    def _kms_crypto_key_path(self) -> str:
        from google.cloud import kms

        client = kms.KeyManagementServiceClient()
        return client.crypto_key_path(
            settings.gcp_project,
            settings.gcp_location,
            settings.trigger_event_kms_keyring,
            settings.trigger_event_kms_key,
        )

    def _derive_fernet_key(self, *, info: bytes) -> bytes:
        assert self._master_key is not None
        derived = HKDF(
            algorithm=SHA256(),
            length=32,
            salt=b"trigger-event-wrap",
            info=info,
        ).derive(self._master_key)
        return base64.urlsafe_b64encode(derived)

    def generate_binding_hmac_key(self) -> bytes:
        """Create a new immutable 256-bit binding HMAC key."""

        return secrets.token_bytes(32)

    def wrap_key(self, *, purpose: str, key_material: bytes) -> WrappedKeyMaterial:
        """Wrap a symmetric key for durable storage."""

        if self._use_kms:
            from google.cloud import kms

            client = kms.KeyManagementServiceClient()
            response = client.encrypt(
                request={
                    "name": self._kms_crypto_key_path(),
                    "plaintext": key_material,
                    "additional_authenticated_data": purpose.encode("utf-8"),
                },
            )
            return WrappedKeyMaterial(
                wrapping_key_version=self.wrapping_key_version,
                wrapped_key=response.ciphertext,
                algorithm=KMS_WRAP_ALGORITHM,
            )

        fernet = Fernet(self._derive_fernet_key(info=purpose.encode("utf-8")))
        return WrappedKeyMaterial(
            wrapping_key_version=self.wrapping_key_version,
            wrapped_key=fernet.encrypt(key_material),
            algorithm=SELF_HOST_WRAP_ALGORITHM,
        )

    def unwrap_key(self, wrapped: WrappedKeyMaterial, *, purpose: str) -> bytes:
        """Recover wrapped symmetric key bytes."""

        if wrapped.algorithm == KMS_WRAP_ALGORITHM:
            from google.cloud import kms

            client = kms.KeyManagementServiceClient()
            response = client.decrypt(
                request={
                    "name": self._kms_crypto_key_path(),
                    "ciphertext": wrapped.wrapped_key,
                    "additional_authenticated_data": purpose.encode("utf-8"),
                },
            )
            return response.plaintext

        assert self._master_key is not None
        fernet = Fernet(self._derive_fernet_key(info=purpose.encode("utf-8")))
        try:
            return fernet.decrypt(wrapped.wrapped_key)
        except InvalidToken as exc:
            raise EventBlobAuthenticationError(
                "wrapped key authentication failed",
            ) from exc

    def rewrap(self, wrapped: WrappedKeyMaterial, *, purpose: str) -> WrappedKeyMaterial:
        """Rewrap a symmetric key under the current wrapping key version."""

        key_material = self.unwrap_key(wrapped, purpose=purpose)
        return self.wrap_key(purpose=purpose, key_material=key_material)


class PrivateObjectStore(ABC):
    """Storage backend for private provider-event ciphertext objects."""

    @abstractmethod
    def put(self, *, namespace_key: str, ciphertext: bytes) -> None:
        """Persist ciphertext bytes for one namespace key."""

    @abstractmethod
    def get(self, *, namespace_key: str) -> bytes:
        """Load ciphertext bytes for one namespace key."""

    @abstractmethod
    def delete(self, *, namespace_key: str) -> None:
        """Remove ciphertext bytes for one namespace key."""

    @abstractmethod
    def stream(self, *, namespace_key: str, chunk_size: int = 65536) -> Iterator[bytes]:
        """Stream ciphertext bytes for one namespace key."""


class FilesystemPrivateObjectStore(PrivateObjectStore):
    """Self-host private object store on a dedicated filesystem root."""

    def __init__(self, root_dir: Path) -> None:
        self._root_dir = root_dir
        self._root_dir.mkdir(parents=True, exist_ok=True)

    def _object_path(self, namespace_key: str) -> Path:
        candidate = (self._root_dir / namespace_key).resolve()
        root = self._root_dir.resolve()
        if root not in candidate.parents and candidate != root:
            raise ValueError("invalid namespace_key path traversal")
        return candidate

    def put(self, *, namespace_key: str, ciphertext: bytes) -> None:
        object_path = self._object_path(namespace_key)
        object_path.parent.mkdir(parents=True, exist_ok=True)
        object_path.write_bytes(ciphertext)

    def get(self, *, namespace_key: str) -> bytes:
        return self._object_path(namespace_key).read_bytes()

    def delete(self, *, namespace_key: str) -> None:
        object_path = self._object_path(namespace_key)
        if object_path.exists():
            object_path.unlink()

    def stream(self, *, namespace_key: str, chunk_size: int = 65536) -> Iterator[bytes]:
        object_path = self._object_path(namespace_key)
        with object_path.open("rb") as handle:
            while True:
                chunk = handle.read(chunk_size)
                if not chunk:
                    break
                yield chunk


class GcsPrivateObjectStore(PrivateObjectStore):
    """Hosted private object store in a dedicated GCS bucket."""

    def __init__(self, *, bucket_name: str) -> None:
        from google.cloud import storage

        self._bucket_name = bucket_name
        self._client = storage.Client(project=settings.gcp_project)
        self._bucket = self._client.bucket(bucket_name)

    def put(self, *, namespace_key: str, ciphertext: bytes) -> None:
        blob = self._bucket.blob(namespace_key)
        blob.upload_from_string(ciphertext)

    def get(self, *, namespace_key: str) -> bytes:
        blob = self._bucket.blob(namespace_key)
        return blob.download_as_bytes()

    def delete(self, *, namespace_key: str) -> None:
        blob = self._bucket.blob(namespace_key)
        if blob.exists():
            blob.delete()

    def stream(self, *, namespace_key: str, chunk_size: int = 65536) -> Iterator[bytes]:
        blob = self._bucket.blob(namespace_key)
        handle = blob.open("rb")
        try:
            while True:
                chunk = handle.read(chunk_size)
                if not chunk:
                    break
                yield chunk
        finally:
            handle.close()


class EventBlobService:
    """Private application-level encrypted blob store for matched provider events."""

    def __init__(
        self,
        *,
        object_store: PrivateObjectStore,
        wrapping_service: TriggerKeyWrappingService,
    ) -> None:
        self._object_store = object_store
        self._wrapping = wrapping_service

    @property
    def bucket_name(self) -> str:
        return PRIVATE_EVENT_BUCKET_NAME

    @property
    def wrapping_service(self) -> TriggerKeyWrappingService:
        return self._wrapping

    def encrypt_object(
        self,
        *,
        binding_id: str,
        receipt_id: str,
        plaintext: bytes,
        content_type: str = "application/json",
    ) -> tuple[EncryptedEventObject, bytes]:
        """Encrypt one provider-event payload without persisting it."""

        purpose = blob_wrap_purpose(binding_id=binding_id, receipt_id=receipt_id)
        associated_data = blob_associated_data(
            binding_id=binding_id,
            receipt_id=receipt_id,
        )
        data_key = secrets.token_bytes(32)
        nonce = secrets.token_bytes(_NONCE_LENGTH)
        ciphertext_body = AESGCM(data_key).encrypt(associated_data, plaintext, nonce)
        ciphertext = nonce + ciphertext_body
        wrapped = self._wrapping.wrap_key(purpose=purpose, key_material=data_key)
        namespace_key = namespace_key_for(binding_id=binding_id, receipt_id=receipt_id)
        encrypted = EncryptedEventObject(
            namespace_key=namespace_key,
            algorithm=AES_GCM_ALGORITHM,
            wrap_algorithm=wrapped.algorithm,
            wrapping_key_version=wrapped.wrapping_key_version,
            wrapped_data_key=wrapped.wrapped_key,
            integrity_hash=hashlib.sha256(ciphertext).hexdigest(),
            size_bytes=len(plaintext),
            content_type=content_type,
        )
        return encrypted, ciphertext

    def write_ciphertext(
        self,
        encrypted: EncryptedEventObject,
        *,
        ciphertext: bytes,
    ) -> None:
        """Persist ciphertext for one encrypted object."""

        if hashlib.sha256(ciphertext).hexdigest() != encrypted.integrity_hash:
            raise EventBlobAuthenticationError("ciphertext integrity check failed")
        self._object_store.put(namespace_key=encrypted.namespace_key, ciphertext=ciphertext)

    def encrypt_and_store(
        self,
        *,
        binding_id: str,
        receipt_id: str,
        plaintext: bytes,
        content_type: str = "application/json",
    ) -> EncryptedEventObject:
        """Encrypt and persist one provider-event payload."""

        encrypted, ciphertext = self.encrypt_object(
            binding_id=binding_id,
            receipt_id=receipt_id,
            plaintext=plaintext,
            content_type=content_type,
        )
        self.write_ciphertext(encrypted, ciphertext=ciphertext)
        return encrypted

    def read_object(
        self,
        encrypted: EncryptedEventObject,
        *,
        binding_id: str,
        receipt_id: str,
    ) -> bytes:
        """Decrypt one stored provider-event object."""

        ciphertext = self._object_store.get(namespace_key=encrypted.namespace_key)
        if hashlib.sha256(ciphertext).hexdigest() != encrypted.integrity_hash:
            raise EventBlobAuthenticationError("event blob integrity check failed")
        return self._decrypt_ciphertext(
            encrypted=encrypted,
            binding_id=binding_id,
            receipt_id=receipt_id,
            ciphertext=ciphertext,
        )

    def stream_object(
        self,
        encrypted: EncryptedEventObject,
        *,
        binding_id: str,
        receipt_id: str,
    ) -> bytes:
        """Load and decrypt one stored provider-event object."""

        return self.read_object(
            encrypted=encrypted,
            binding_id=binding_id,
            receipt_id=receipt_id,
        )

    def rewrap_object_metadata(
        self,
        encrypted: EncryptedEventObject,
        *,
        binding_id: str,
        receipt_id: str,
    ) -> EncryptedEventObject:
        """Rewrap the data key without changing stored ciphertext or plaintext."""

        purpose = blob_wrap_purpose(binding_id=binding_id, receipt_id=receipt_id)
        wrapped = WrappedKeyMaterial(
            wrapping_key_version=encrypted.wrapping_key_version,
            wrapped_key=encrypted.wrapped_data_key,
            algorithm=encrypted.wrap_algorithm,
        )
        rewrapped = self._wrapping.rewrap(wrapped, purpose=purpose)
        return EncryptedEventObject(
            namespace_key=encrypted.namespace_key,
            algorithm=encrypted.algorithm,
            wrap_algorithm=rewrapped.algorithm,
            wrapping_key_version=rewrapped.wrapping_key_version,
            wrapped_data_key=rewrapped.wrapped_key,
            integrity_hash=encrypted.integrity_hash,
            size_bytes=encrypted.size_bytes,
            content_type=encrypted.content_type,
        )

    def delete_ciphertext(self, *, namespace_key: str) -> None:
        """Remove stored ciphertext for one namespace key."""

        self._object_store.delete(namespace_key=namespace_key)

    def _decrypt_ciphertext(
        self,
        *,
        encrypted: EncryptedEventObject,
        binding_id: str,
        receipt_id: str,
        ciphertext: bytes,
    ) -> bytes:
        if len(ciphertext) <= _NONCE_LENGTH:
            raise EventBlobAuthenticationError("event blob ciphertext is too short")
        purpose = blob_wrap_purpose(binding_id=binding_id, receipt_id=receipt_id)
        associated_data = blob_associated_data(
            binding_id=binding_id,
            receipt_id=receipt_id,
        )
        wrapped = WrappedKeyMaterial(
            wrapping_key_version=encrypted.wrapping_key_version,
            wrapped_key=encrypted.wrapped_data_key,
            algorithm=encrypted.wrap_algorithm,
        )
        data_key = self._wrapping.unwrap_key(wrapped, purpose=purpose)
        nonce = ciphertext[:_NONCE_LENGTH]
        ciphertext_body = ciphertext[_NONCE_LENGTH:]
        try:
            return AESGCM(data_key).decrypt(associated_data, ciphertext_body, nonce)
        except InvalidTag as exc:
            raise EventBlobAuthenticationError(
                "event blob authentication failed",
            ) from exc


def create_private_object_store() -> PrivateObjectStore:
    """Build the configured private object store backend."""

    if settings.is_self_host:
        root = Path(settings.trigger_event_private_root).expanduser()
        return FilesystemPrivateObjectStore(root)
    if not settings.gcp_project:
        raise ProviderEventStorageUnavailableError(
            "hosted private event storage requires GCP project configuration",
        )
    return GcsPrivateObjectStore(bucket_name=settings.trigger_event_private_bucket)


def create_trigger_key_wrapping_service(
    *,
    master_key: bytes | None = None,
) -> TriggerKeyWrappingService:
    """Build the configured key-wrapping service."""

    if not TriggerKeyWrappingService.is_configured():
        raise ProviderEventStorageUnavailableError(
            "provider-event storage prerequisites are not configured",
        )
    return TriggerKeyWrappingService(master_key=master_key)


def create_event_blob_service(
    *,
    master_key: bytes | None = None,
    object_store: PrivateObjectStore | None = None,
) -> EventBlobService:
    """Build a configured private event blob service."""

    wrapping = create_trigger_key_wrapping_service(master_key=master_key)
    store = object_store or create_private_object_store()
    return EventBlobService(object_store=store, wrapping_service=wrapping)


def provider_event_storage_configured() -> bool:
    """Return True when private provider-event storage can operate."""

    if not TriggerKeyWrappingService.is_configured():
        return False
    if settings.is_self_host:
        return bool(settings.trigger_event_private_root)
    if not (settings.gcp_project and settings.trigger_event_private_bucket):
        return False
    from orchestra.services.local_bucket_service import LocalBucketService

    return not LocalBucketService().is_allowed_bucket(
        settings.trigger_event_private_bucket,
    )
