"""Provider-event run-key construction shared with Unity's contract shape."""

from __future__ import annotations

import hashlib
import re
from typing import Literal

_RUN_KEY_SAFE_RE = re.compile(r"[^a-z0-9-]+")


def normalize_run_key_component(value: str) -> str:
    """Normalize one free-form identifier into a run-key segment."""

    normalised = _RUN_KEY_SAFE_RE.sub("-", value.lower()).strip("-")
    return normalised or "assistant"


def build_provider_event_run_key(
    *,
    assistant_id: str,
    task_id: int,
    binding_id: str,
    activation_revision: str,
    event_identity_hmac: str,
    execution_mode: Literal["live", "offline"] = "offline",
) -> str:
    """Build the deterministic provider-event run key.

    Unlike communication-trigger keys, the provider event identity digest is
    included in full so two identities that share a 12-hex prefix cannot
    collide through truncation.
    """

    revision_digest = hashlib.sha256(
        str(activation_revision or "").encode("utf-8"),
    ).hexdigest()[:12]
    binding_part = normalize_run_key_component(binding_id)
    identity = str(event_identity_hmac).strip()
    if not identity:
        raise ValueError("event_identity_hmac is required")
    return (
        f"{execution_mode}:provider_event:{assistant_id}:{task_id}:"
        f"{binding_part}:{revision_digest}:{identity}"
    )
