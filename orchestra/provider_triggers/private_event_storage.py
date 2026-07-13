"""Private encrypted storage for provider-event payloads."""

from __future__ import annotations

import base64
import hashlib
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

PRIVATE_EVENT_BUCKET_NAME: Final[str] = "provider-event-blobs"
_SAFE_OBJECT_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _validated_object_segment(value: str, *, field_name: str) -> str:
    if not _SAFE_OBJECT_SEGMENT.fullmatch(value):
        raise ValueError(f"invalid {field_name} for private event storage")
    return value


def _fernet_key_from_material(material: bytes) -> bytes:
    """Build a Fernet-compatible key from 32 bytes of key material."""

    return base64.urlsafe_b64encode(material[:32].ljust(32, b"\0"))


@dataclass(frozen=True)
class WrappedKeyMaterial:
    """Metadata for a wrapped symmetric key."""

    wrapping_key_version: str
    wrapped_key: bytes
    algorithm: str = "fernet-hkdf-v1"


class TriggerKeyWrappingService:
    """Wrap binding HMAC keys and per-object data keys for private event storage."""

    def __init__(self, *, master_key: bytes | None = None) -> None:
        if master_key is None:
            configured = os.getenv("TRIGGER_EVENT_WRAPPING_MASTER_KEY", "").strip()
            if not configured:
                raise ValueError(
                    "TRIGGER_EVENT_WRAPPING_MASTER_KEY is required for private event storage",
                )
            material = configured.encode("utf-8")
        else:
            material = master_key
        self._master_key = material
        self.wrapping_key_version = hashlib.sha256(material).hexdigest()[:16]

    def _derive_fernet_key(self, *, info: bytes) -> bytes:
        derived = HKDF(
            algorithm=SHA256(),
            length=32,
            salt=None,
            info=info,
        ).derive(self._master_key)
        return _fernet_key_from_material(derived)

    def generate_binding_hmac_key(self) -> bytes:
        """Create a new immutable 256-bit binding HMAC key."""

        return secrets.token_bytes(32)

    def wrap_key(self, *, purpose: str, key_material: bytes) -> WrappedKeyMaterial:
        """Wrap a symmetric key for durable storage."""

        fernet = Fernet(self._derive_fernet_key(info=purpose.encode("utf-8")))
        return WrappedKeyMaterial(
            wrapping_key_version=self.wrapping_key_version,
            wrapped_key=fernet.encrypt(key_material),
        )

    def unwrap_key(self, wrapped: WrappedKeyMaterial, *, purpose: str) -> bytes:
        """Recover wrapped symmetric key bytes."""

        fernet = Fernet(self._derive_fernet_key(info=purpose.encode("utf-8")))
        return fernet.decrypt(wrapped.wrapped_key)


@dataclass(frozen=True)
class StoredEventBlob:
    """Metadata for one encrypted provider-event object."""

    namespace_key: str
    wrapping_key_version: str
    wrapped_data_key: bytes
    ciphertext: bytes
    integrity_hash: str
    size_bytes: int
    content_type: str


class EventBlobService:
    """Private application-level encrypted blob store for matched provider events."""

    def __init__(
        self,
        *,
        root_dir: Path,
        wrapping_service: TriggerKeyWrappingService,
    ) -> None:
        self._root_dir = root_dir
        self._wrapping = wrapping_service
        self._root_dir.mkdir(parents=True, exist_ok=True)

    @property
    def bucket_name(self) -> str:
        return PRIVATE_EVENT_BUCKET_NAME

    def write_object(
        self,
        *,
        binding_id: str,
        receipt_id: str,
        plaintext: bytes,
        content_type: str = "application/json",
    ) -> StoredEventBlob:
        """Encrypt and persist one provider-event payload."""

        _validated_object_segment(binding_id, field_name="binding_id")
        _validated_object_segment(receipt_id, field_name="receipt_id")
        data_key = secrets.token_bytes(32)
        fernet = Fernet(_fernet_key_from_material(data_key))
        ciphertext = fernet.encrypt(plaintext)
        wrapped = self._wrapping.wrap_key(
            purpose=f"blob:{binding_id}:{receipt_id}",
            key_material=data_key,
        )
        namespace_key = f"{binding_id}/{receipt_id}.bin"
        object_path = self._root_dir / namespace_key
        object_path.parent.mkdir(parents=True, exist_ok=True)
        object_path.write_bytes(ciphertext)
        integrity = hashlib.sha256(ciphertext).hexdigest()
        return StoredEventBlob(
            namespace_key=namespace_key,
            wrapping_key_version=wrapped.wrapping_key_version,
            wrapped_data_key=wrapped.wrapped_key,
            ciphertext=ciphertext,
            integrity_hash=integrity,
            size_bytes=len(plaintext),
            content_type=content_type,
        )

    def read_object(
        self,
        stored: StoredEventBlob,
        *,
        binding_id: str,
        receipt_id: str,
    ) -> bytes:
        """Decrypt one stored provider-event object."""

        _validated_object_segment(binding_id, field_name="binding_id")
        _validated_object_segment(receipt_id, field_name="receipt_id")
        ciphertext = self.local_object_path(stored.namespace_key).read_bytes()
        if hashlib.sha256(ciphertext).hexdigest() != stored.integrity_hash:
            raise ValueError("event blob integrity check failed")
        data_key = self._wrapping.unwrap_key(
            WrappedKeyMaterial(
                wrapping_key_version=stored.wrapping_key_version,
                wrapped_key=stored.wrapped_data_key,
            ),
            purpose=f"blob:{binding_id}:{receipt_id}",
        )
        fernet = Fernet(_fernet_key_from_material(data_key))
        try:
            return fernet.decrypt(ciphertext)
        except InvalidToken as exc:
            raise ValueError("event blob authentication failed") from exc

    def local_object_path(self, namespace_key: str) -> Path:
        return self._root_dir / namespace_key
