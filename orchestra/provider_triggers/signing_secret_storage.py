"""Wrap and unwrap per-generation provider webhook signing secrets."""

from __future__ import annotations

import base64

from orchestra.provider_triggers.private_event_storage import (
    WrappedKeyMaterial,
    create_trigger_key_wrapping_service,
)


def signing_secret_wrap_purpose(*, generation_id: str) -> str:
    """Return the key-wrapping purpose for one subscription generation."""

    return f"signing-secret:{generation_id}"


def wrap_signing_secret_for_generation(
    *,
    generation_id: str,
    signing_key: str,
) -> tuple[str, str]:
    """Wrap one raw signing key and return a durable reference plus version."""

    wrapping = create_trigger_key_wrapping_service()
    wrapped = wrapping.wrap_key(
        purpose=signing_secret_wrap_purpose(generation_id=generation_id),
        key_material=signing_key.encode("utf-8"),
    )
    ref = (
        f"wrapped:{generation_id}:{wrapped.algorithm}:"
        f"{wrapped.wrapping_key_version}:"
        f"{base64.b64encode(wrapped.wrapped_key).decode('ascii')}"
    )
    return ref, wrapped.wrapping_key_version


def unwrap_signing_secret_ref(secret_ref: str) -> str | None:
    """Recover plaintext signing material from one wrapped reference."""

    if not secret_ref.startswith("wrapped:"):
        return None
    parts = secret_ref.split(":", 4)
    if len(parts) != 5:
        return None
    _, generation_id, algorithm, version, encoded = parts
    if not generation_id or not algorithm or not version or not encoded:
        return None
    try:
        ciphertext = base64.b64decode(encoded)
    except ValueError:
        return None
    wrapping = create_trigger_key_wrapping_service()
    plaintext = wrapping.unwrap_key(
        WrappedKeyMaterial(
            wrapping_key_version=version,
            wrapped_key=ciphertext,
            algorithm=algorithm,
        ),
        purpose=signing_secret_wrap_purpose(generation_id=generation_id),
    )
    return plaintext.decode("utf-8")
